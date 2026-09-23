import json
import os
import tempfile
import threading
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import DetailView

from common import is_id

from . import supplier_changes
from .deletion import InvoiceInUseError, blocking_stock_takes, delete_invoice
from .einvoice import NO_LINES_CHECK as EINVOICE_NO_LINES
from .forms import (
    DOCUMENT_INVOICE,
    DOCUMENT_RECEIPT,
    DocumentHeaderForm,
    EmailInvoiceSourceForm,
    EmptyVatTableFormSet,
    InvoiceTypeForm,
    InvoiceUploadForm,
    LineCorrectionFormSet,
    ManualInvoiceForm,
    ManualInvoiceLineFormSet,
    ReceiptBatchUploadForm,
    ReceiptShopForm,
    ShopItemPriceForm,
    VatTableFormSet,
    WebsiteInvoiceSourceForm,
    line_initial,
)
from .importing import (
    DuplicateInvoiceError,
    InvoiceLinesInUseError,
    corrected_line,
    import_parsed_invoice,
    parse_and_import,
    replace_invoice_lines,
)
from .models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier
from .parsers import LLM_PARSER_KEY, get_parser
from .parsers.base import ParsedInvoice, ParsedLine
from .tasks import gather_invoices_task, test_email_pattern_task
from .workspace import batch_invoice_ids, batch_status_context, render_purchases


def invoice_list(request):
    """"Achats", on its list of every document."""
    return render_purchases(request, "documents")


def _documents_changed(response):
    """Tell the page to reload its list of documents: an import ended."""
    response["HX-Trigger"] = json.dumps({"documents-changed": True})
    return response


class InvoiceDetailView(DetailView):
    model = Invoice
    template_name = "invoices/invoice_detail.html"
    context_object_name = "invoice"

    def get_queryset(self):
        return Invoice.objects.select_related("supplier").prefetch_related("lines__product__stock_type")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Handed to the template as a list so the totals in the footer and
        # the rows above them read from one prefetched set rather than
        # re-querying per property.
        context["lines"] = list(self.object.lines.all())
        context["has_parser"] = get_parser(self.object.supplier.parser_key) is not None
        invoice = self.object
        # See _correction_page for both: « Avoir » was a word no screen said,
        # and a rebuilt MINIMUM line is not an article.
        context["einvoice_is_credit"] = (
            invoice.is_einvoice
            and invoice.printed_total_ttc is not None
            and invoice.printed_total_ttc < 0
        )
        context["einvoice_no_lines"] = invoice.is_einvoice and any(
            check["label"] == EINVOICE_NO_LINES for check in invoice.parse_checks
        )
        return context


def upload_invoice(request):
    """A supplier's PDF, from the import card. With a reader of its own, it is
    shown first in the list, highlighted and opened on its lines. Any other
    supplier's - a new one's included - is read like a ticket and opens on
    the correction page, beside its PDF."""
    from .receipts import OCR_LOCK, OCR_WAIT_SECONDS, import_document

    if request.method != "POST":
        return render_purchases(request, "documents", import_tab="pdf")
    form = InvoiceUploadForm(request.POST, request.FILES)
    supplier = created = None
    if form.is_valid():
        try:
            supplier, created = form.shop()
        except ValueError as exc:
            form.add_error("new_name", str(exc))
    if supplier is None:
        return render_purchases(request, "documents", import_tab="pdf", pdf_form=form)
    uploaded = form.cleaned_data["source_file"]
    suffix = os.path.splitext(uploaded.name)[1] or ".pdf"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as tmp:
            for chunk in uploaded.chunks():
                tmp.write(chunk)
        if supplier.parser_key == LLM_PARSER_KEY:
            invoice = parse_and_import(tmp_path, supplier, display_filename=uploaded.name)
        else:
            # A scan is OCR, seconds of CPU: one document at a time. The file
            # decides the reader here too - a supplier's own when it has one
            # and the document is digital, the ticket reader otherwise.
            if not OCR_LOCK.acquire(timeout=OCR_WAIT_SECONDS):
                raise RuntimeError("un autre document est en cours de lecture, réessayez dans un instant")
            try:
                with supplier_changes.cause(
                    f"import de {uploaded.name}, {supplier.name} choisi", by_person=True
                ), supplier_changes.collect() as changes:
                    invoice = import_document(
                        tmp_path,
                        display_filename=uploaded.name,
                        supplier=supplier,
                        chosen_because=f"Fournisseur choisi à l'import de la facture ({supplier.name}).",
                    )
                _say_supplier_changes(request, changes)
            finally:
                OCR_LOCK.release()
    except DuplicateInvoiceError as exc:
        messages.warning(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not a crash
        messages.error(request, f"Échec de l'import : {exc}")
    else:
        if created:
            messages.info(request, f"Fournisseur {supplier.name} créé.")
        # Read by its supplier's own reader, it goes to the list; read the
        # way a ticket is, to the screen where it is checked against itself.
        if not invoice.is_receipt:
            messages.success(request, f"Facture importée : {invoice}. Ses lignes sont ouvertes dans la liste.")
            return redirect(f"{reverse('invoices:invoice_list')}?surligner={invoice.pk}")
        messages.success(
            request, f"Facture {supplier.name} lue sans lecteur dédié : vérifiez ses lignes d'après le document."
        )
        return redirect("invoices:receipt_review", pk=invoice.pk)
    finally:
        os.unlink(tmp_path)
    return redirect(f"{reverse('invoices:invoice_list')}?ajouter=pdf")


def invoice_preview(request, pk):
    """A document's lines, opened in place under its row in the list."""
    invoice = get_object_or_404(
        Invoice.objects.select_related("supplier").prefetch_related("lines__product__stock_type"), pk=pk
    )
    lines = list(invoice.lines.all())
    return render(
        request,
        "invoices/_document_preview.html",
        {
            "invoice": invoice,
            "lines": lines,
            "to_classify": sum(1 for line in lines if line.product.needs_review),
        },
    )


def create_manual_invoice(request):
    if request.method == "POST":
        form = ManualInvoiceForm(request.POST, request.FILES)
        formset = ManualInvoiceLineFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            lines = []
            for line_form in formset:
                if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                    continue
                quantity = line_form.cleaned_data["quantity"]
                total_ht = line_form.cleaned_data["total_ht"]
                lines.append(
                    ParsedLine(
                        raw_name=line_form.cleaned_data["product_name"],
                        quantity=quantity,
                        total_volume=Decimal("0"),
                        unit_cost_ht=(total_ht / quantity).quantize(Decimal("0.0001")) if quantity else Decimal("0"),
                        total_ht=total_ht,
                        vat_rate=line_form.cleaned_data["vat_rate"] / Decimal("100"),
                    )
                )
            supplier = form.cleaned_data["supplier"]
            parsed = ParsedInvoice(
                supplier_code=supplier.code,
                invoice_number=form.cleaned_data["invoice_number"],
                invoice_date=form.cleaned_data["invoice_date"],
                lines=lines,
            )
            uploaded = form.cleaned_data.get("source_file")
            tmp_path = None
            try:
                if uploaded:
                    suffix = os.path.splitext(uploaded.name)[1] or ".pdf"
                    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                    with os.fdopen(fd, "wb") as tmp:
                        for chunk in uploaded.chunks():
                            tmp.write(chunk)
                invoice = import_parsed_invoice(
                    supplier,
                    parsed,
                    source_file_path=tmp_path,
                    display_filename=uploaded.name if uploaded else None,
                )
                messages.success(request, f"Facture créée : {invoice}")
                return redirect("invoices:invoice_detail", pk=invoice.pk)
            except DuplicateInvoiceError as exc:
                messages.warning(request, str(exc))
            finally:
                if tmp_path:
                    os.unlink(tmp_path)
    else:
        form = ManualInvoiceForm()
        formset = ManualInvoiceLineFormSet()
    return render(request, "invoices/manual_invoice_form.html", {"form": form, "formset": formset})


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def trigger_gather(request):
    if request.method != "POST":
        return redirect("invoices:invoice_list")

    # Clear out any run that died without saying so before deciding whether
    # one is genuinely in progress - otherwise a single killed thread locks
    # this page out permanently. See common.JobLogMixin.
    ScrapeJob.reap_stale()
    active_job = ScrapeJob.objects.filter(
        kind=ScrapeJob.Kind.GATHER, status__in=[ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING]
    ).first()
    source_codes = set(request.POST.getlist("sources"))
    # One sign-in to a paused Metro, asked for on purpose (its own box is
    # disabled while paused, so this names it).
    metro_now = request.POST.get("metro_now") == "on"
    if metro_now:
        source_codes.add("METRO")
    if active_job is not None:
        messages.info(request, "Une récupération est déjà en cours : elle s'affiche ci-dessous.")
    elif not source_codes:
        # It ran, searched nothing and said "Terminé".
        messages.error(request, "Aucune source cochée : rien à récupérer. Cochez-en au moins une sous « Sources ».")
    else:
        start_date = _parse_date(request.POST.get("start_date"))
        end_date = _parse_date(request.POST.get("end_date"))
        active_job = ScrapeJob.objects.create(range_start=start_date, range_end=end_date)
        thread = threading.Thread(
            target=gather_invoices_task,
            args=(active_job.id, start_date, end_date, source_codes, metro_now),
            daemon=True,
        )
        thread.start()
    return redirect(f"{reverse('invoices:invoice_list')}?ajouter=recuperer")


def gather_status(request, job_id):
    # A run whose thread died (the dev server reloading) is said to have
    # stopped here, where the page asks - nothing else would, and its card
    # polled, and its button stayed disabled, for good.
    ScrapeJob.reap_stale()
    job = get_object_or_404(ScrapeJob, pk=job_id)
    response = render(request, "invoices/_gather_status.html", {"job": job})
    # Polled only while the gather runs: a finished answer is its end.
    return response if job.is_active else _documents_changed(response)


def cancel_gather(request, job_id):
    if request.method != "POST":
        return redirect("invoices:invoice_list")
    job = get_object_or_404(ScrapeJob, pk=job_id)
    if job.status in (ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING):
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
    # Renders the same partial gather_status does (rather than redirecting)
    # so the htmx-powered "Annuler" button can swap it in directly, whether
    # the job being cancelled is a real gather (the Achats page) or a
    # pattern test (invoice_type_form.html) - both already include this
    # same partial for their live status card.
    return render(request, "invoices/_gather_status.html", {"job": job})


def invoice_type_list(request):
    """"Achats", on where invoices come from."""
    return render_purchases(request, "sources")


def invoice_type_form(request, pk=None):
    """A kind of invoice to gather: from the shared mailbox (patterns on
    the emails) or from the supplier's customer portal (a login page and
    the names of the .env variables holding the credentials). Only the
    chosen kind's settings are validated and saved; "Tester" runs either
    without saving and without importing anything.

    Its supplier can be a new one, made in the same save (and not at all if
    the type is refused). `?fournisseur=` and `?source=` fill the page in
    from a supplier's page, and `?retour=` is where it goes back after."""
    from .receipts import invoice_supplier_choices
    from .supplier_views import _local_return

    invoice_type = get_object_or_404(InvoiceType, pk=pk) if pk else None
    source = getattr(invoice_type, "email_source", None) if invoice_type else None
    website = getattr(invoice_type, "website_source", None) if invoice_type else None
    test_job = None
    retour = _local_return(request)

    if request.method == "POST":
        type_form = InvoiceTypeForm(request.POST, instance=invoice_type)
        is_website = request.POST.get("source_kind") == InvoiceType.SourceKind.WEBSITE
        # Only the chosen kind's fields are sent (the other's are disabled in
        # the page); the other kind is shown again as saved, not blank.
        if is_website:
            source_form = EmailInvoiceSourceForm(
                instance=source,
                initial={
                    "test_start_date": request.POST.get("test_start_date"),
                    "test_end_date": request.POST.get("test_end_date"),
                },
            )
            website_form = WebsiteInvoiceSourceForm(request.POST, instance=website, prefix="site")
        else:
            source_form = EmailInvoiceSourceForm(request.POST, instance=source)
            website_form = WebsiteInvoiceSourceForm(instance=website, prefix="site")

        if is_website:
            if request.POST.get("action") == "test":
                test_job = _test_website(request, type_form, website_form)
            elif type_form.is_valid() and website_form.is_valid():
                saved = _save_invoice_type(request, type_form, invoice_type, website_form, "website", retour)
                if saved is not None:
                    return saved
        elif request.POST.get("action") == "test":
            # Only the patterns need to be valid to try them - name/supplier
            # can still be blank/invalid while iterating on a regex.
            if source_form.is_valid():
                start = source_form.cleaned_data["test_start_date"] or (timezone.localdate() - timedelta(days=30))
                end = source_form.cleaned_data["test_end_date"] or timezone.localdate()
                test_job = ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST)
                thread = threading.Thread(
                    target=test_email_pattern_task,
                    args=(
                        test_job.id,
                        start,
                        end,
                        source_form.cleaned_data["sender_pattern"],
                        source_form.cleaned_data["subject_pattern"],
                        source_form.cleaned_data["body_pattern"],
                        source_form.cleaned_data["attachment_pattern"],
                    ),
                    daemon=True,
                )
                thread.start()
        else:
            if type_form.is_valid() and source_form.is_valid():
                saved = _save_invoice_type(request, type_form, invoice_type, source_form, "email", retour)
                if saved is not None:
                    return saved
    else:
        prefill = {}
        if invoice_type is None and is_id(request.GET.get("fournisseur", "")):
            prefill["supplier"] = request.GET["fournisseur"]
        if invoice_type is None and request.GET.get("source") in InvoiceType.SourceKind.values:
            prefill["source_kind"] = request.GET["source"]
        type_form = InvoiceTypeForm(instance=invoice_type, initial=prefill)
        source_form = EmailInvoiceSourceForm(
            instance=source,
            initial={
                "test_start_date": timezone.localdate() - timedelta(days=30),
                "test_end_date": timezone.localdate(),
            },
        )
        website_form = WebsiteInvoiceSourceForm(instance=website, prefix="site")

    return render(
        request,
        "invoices/invoice_type_form.html",
        {
            "type_form": type_form,
            "source_form": source_form,
            "website_form": website_form,
            "is_website": (type_form["source_kind"].value() or "") == InvoiceType.SourceKind.WEBSITE,
            "test_job": test_job,
            "invoice_type": invoice_type,
            # Rendered by hand beside the "Tester" button rather than among
            # the pattern fields, so the shared partial leaves them out.
            "test_date_fields": ["test_start_date", "test_end_date"],
            "supplier_groups": invoice_supplier_choices(),
            "supplier_selected": str(type_form["supplier"].value() or ""),
            "typed_name": type_form["new_name"].value() or "",
            "typed_expenses": bool(type_form["new_expenses"].value()),
            "retour": retour,
            # What the page was drawn with, carried through a redraw (« Tester
            # », an error): taken from the database again, a redrawn page
            # moved the type back unrefused.
            "supplier_was": _supplier_was(request, type_form, invoice_type),
        },
    )


def _supplier_was(request, type_form, invoice_type):
    if invoice_type is None:
        return ""
    seen = getattr(type_form, "supplier_seen", None)
    if seen is not None:
        return seen
    posted = request.POST.get("supplier_was", "") if request.method == "POST" else ""
    return int(posted) if is_id(posted) else invoice_type.supplier_id


def _save_invoice_type(request, type_form, invoice_type, source_form, kind, retour):
    """The type and its source saved - with its supplier, made now if it is
    a new one, all in one transaction: a refusal leaves nothing. A type
    moved to another supplier is recorded on both. None when refused (the
    reason is on the form)."""
    from .supplier_views import record_type_moved

    before = invoice_type.supplier if invoice_type is not None else None
    was = request.POST.get("supplier_was", "")
    if before is not None and is_id(was) and int(was) != before.pk:
        # The type moved since this page was drawn (a « Rendre ce type » in
        # another tab, or the type saved from another tab): saved as it was,
        # the page moved it back, unseen.
        type_form.add_error(
            "supplier",
            f"« {invoice_type.name} » récupère désormais pour {before.name}, depuis l'ouverture de cette page : "
            "vérifiez son fournisseur, puis enregistrez de nouveau.",
        )
        # Seen now: the page drawn again carries where it is, and saving it
        # again is a choice made knowingly.
        type_form.supplier_seen = before.pk
        return None
    name = type_form.cleaned_data["name"]
    try:
        with transaction.atomic(), supplier_changes.cause(f"source « {name} »", by_person=True):
            supplier, created = type_form.chosen_supplier()
            saved_type = type_form.save(commit=False)
            saved_type.supplier = supplier
            if kind == "email":
                saved_type.source_kind = InvoiceType.SourceKind.EMAIL
            saved_type.save()
            saved_source = source_form.save(commit=False)
            saved_source.invoice_type = saved_type
            saved_source.save()
            if before is not None and before.pk != supplier.pk:
                record_type_moved(saved_type, before, supplier)
    except ValueError as exc:
        type_form.add_error("new_name", str(exc))
        return None
    messages.success(request, f"Source enregistrée : {saved_type.name}")
    if created:
        messages.info(
            request,
            f"{supplier.name} est créé : cette source range chez lui ce qu'elle récupère, et il apprend ce que "
            "ses documents impriment dès le premier.",
        )
    if before is not None and before.pk != supplier.pk:
        messages.info(
            request,
            f"« {saved_type.name} » récupère désormais pour {supplier.name} (avant : {before.name}). Les documents "
            f"déjà récupérés restent chez {before.name} : s'ils sont de {supplier.name}, changez-les de fournisseur "
            "depuis leur page.",
        )
    return redirect(retour or "invoices:invoice_type_list")


def _test_website(request, type_form, website_form):
    """Sign in on the site with the settings as typed and list what a
    gather would download - nothing saved, nothing downloaded."""
    from .scrapers.website import WebsiteRecipe
    from .tasks import test_website_task

    if not website_form.is_valid():
        return None
    start = _parse_date(request.POST.get("test_start_date")) or (timezone.localdate() - timedelta(days=90))
    end = _parse_date(request.POST.get("test_end_date")) or timezone.localdate()
    site = website_form.save(commit=False)
    name = (type_form.data.get("name") or "").strip() or "Site"
    supplier_id = type_form.data.get("supplier") or ""
    job = ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST)
    thread = threading.Thread(
        target=test_website_task,
        args=(job.id, WebsiteRecipe.from_source(site, name=name), int(supplier_id) if is_id(supplier_id) else 0, start, end),
        daemon=True,
    )
    thread.start()
    return job


def edit_invoice_lines(request, pk):
    """Correct an invoice's lines - or type them in, for a supplier with no
    parser (see importing.parse_and_import). A ticket's are corrected on its
    review screen: the same page, with the ticket's queue around it."""
    invoice = get_object_or_404(Invoice.objects.select_related("supplier"), pk=pk)
    if invoice.is_receipt:
        return redirect("invoices:receipt_review", pk=invoice.pk)
    return _correction_page(request, invoice)


# --------------------------------------------------------------------------
# Photographed till receipts
#
# A separate flow from `upload_invoice` because the two are not the same job.
# A digital invoice arrives once, parses exactly, and is done. A receipt is a
# photo: it arrives in batches, it may be misread, and the point of the
# screens below is to make checking a batch fast enough that it actually gets
# done. See invoices/receipts.py.
# --------------------------------------------------------------------------


def _pending_receipts():
    """Receipts a person still has to look at, oldest first.

    Oldest first on purpose: a review queue is worked through, not browsed,
    and starting at the end means the backlog never shrinks from the front.
    """
    from .receipts import pending_receipts

    return pending_receipts().select_related("supplier").order_by("invoice_date", "id")


def receipt_upload(request):
    """Receipt photos in: a few files, or a whole folder, from the import card.

    The files are staged and handed to a background job
    (invoices/receipt_batches.py) - a folder is minutes of OCR, far too long
    to hold a request open - and the browser goes straight to that import,
    which fills in file by file in the card.
    """
    from .receipt_batches import stage_batch, start_batch

    ReceiptBatch.reap_stale()
    if request.method != "POST":
        return render_purchases(request, "documents", import_tab="tickets")
    form = ReceiptBatchUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return render_purchases(request, "documents", import_tab="tickets", receipt_form=form)
    batch = stage_batch(form.cleaned_data["files"], form.ignored_names)
    start_batch(batch)
    return redirect("invoices:receipt_batch", pk=batch.pk)


def receipt_batch(request, pk):
    """One import: its progress while it runs and every file's outcome in
    the card, its documents in the list below."""
    ReceiptBatch.reap_stale()
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    return render_purchases(request, "documents", import_tab="tickets", batch=batch)


def receipt_batch_status(request, pk):
    """The live part of the batch page, re-fetched by htmx every second while
    the batch runs. It is all that changes on screen, so it has to notice a
    dead batch itself - or "En cours" stays up for ever."""
    ReceiptBatch.reap_stale()
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    response = render(request, "invoices/_receipt_batch_status.html", batch_status_context(batch))
    # Polled only while the import runs: a finished answer is its end, and
    # the list below takes in its documents.
    return response if batch.is_active else _documents_changed(response)


def receipt_batch_resume(request, pk):
    """Carry on with the files an interrupted batch never reached."""
    from .receipt_batches import resume_batch

    batch = get_object_or_404(ReceiptBatch, pk=pk)
    if request.method == "POST":
        resumed = resume_batch(batch)
        if resumed:
            messages.success(request, f"Import repris : {resumed} ticket(s) restant(s) à lire.")
        else:
            messages.warning(
                request,
                "Rien à reprendre : cet import tourne encore (ou vient à peine de s'arrêter - "
                "réessayez dans une minute), ou tous ses tickets ont été lus.",
            )
    return redirect("invoices:receipt_batch", pk=batch.pk)


def receipt_batch_assign(request, pk, index):
    """File a ticket no shop was recognised on under the shop the operator
    names, then open it on the review screen - the same one as every other
    scan, where a ticket that could not be read is typed in from its photo.

    The OCR runs inside this request: one ticket is a few seconds, not the
    minutes a folder takes.
    """
    from .receipt_batches import ShopChoiceError, import_with_shop

    batch = get_object_or_404(ReceiptBatch, pk=pk)
    if request.method != "POST":
        return redirect("invoices:receipt_batch", pk=batch.pk)
    form = ReceiptShopForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.error_text())
        return redirect("invoices:receipt_batch", pk=batch.pk)
    try:
        supplier, created = form.shop()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("invoices:receipt_batch", pk=batch.pk)
    try:
        with supplier_changes.cause(
            f"import d'un fichier du lot, {supplier.name} choisi", by_person=True
        ), supplier_changes.collect() as changes:
            entry = import_with_shop(batch, index, supplier)
    except ShopChoiceError as exc:
        messages.error(request, str(exc))
        return redirect("invoices:receipt_batch", pk=batch.pk)
    if entry["status"] != "ok":
        messages.warning(request, entry["message"])
        return redirect("invoices:receipt_batch", pk=batch.pk)
    messages.success(request, f"{entry['name']} importé comme ticket {supplier.name} : vérifiez-le d'après la photo.")
    _say_supplier_changes(request, changes)
    if created:
        _say_new_shop(request, supplier)
    # Checked within its import - the batch may still be running meanwhile.
    return redirect(reverse("invoices:receipt_review", args=[entry["invoice_id"]]) + f"?lot={batch.pk}")


def _say_new_shop(request, supplier) -> None:
    """A new shop: what files its next documents there. With a header - given
    from the review screen, where the document is on show - the files no shop
    was recognised on are read again, and the tickets filed elsewhere that
    print it are named, to be moved if they are its."""
    from .receipt_batches import requeue_everywhere
    from .receipts import describe_tickets, tickets_printing

    elsewhere = [ticket for ticket in tickets_printing(supplier.ticket_header) if ticket.supplier_id != supplier.pk]
    if elsewhere:
        messages.warning(
            request,
            f"« {supplier.ticket_header} » est aussi imprimé sur {describe_tickets(elsewhere)} : si ce sont des "
            f"documents de {supplier.name}, changez-les de fournisseur depuis la page de chacun.",
        )
    if not supplier.ticket_header:
        messages.info(
            request,
            f"Enseigne {supplier.name} créée. Sur la page du ticket, d'après la photo, indiquez le texte "
            "qu'elle imprime en tête : ses prochains documents y seront rangés tout seuls.",
        )
        return
    requeued = requeue_everywhere()
    messages.info(
        request,
        f"Enseigne {supplier.name} créée : les documents qui portent « {supplier.ticket_header} » y seront rangés"
        + (f" - {requeued} fichier(s) sans enseigne sont relus." if requeued else "."),
    )


def receipt_batch_cancel(request, pk):
    """Stop after the file being read now. What was already imported stays."""
    if request.method != "POST":
        return redirect("invoices:receipt_batch", pk=pk)
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    if batch.is_active:
        batch.cancel_requested = True
        batch.save(update_fields=["cancel_requested"])
    return render(request, "invoices/_receipt_batch_status.html", batch_status_context(batch))


def supplier_expenses(request, pk):
    """Say whether a supplier's documents are charges rather than goods - a
    subscription, the rent, the water. Ticking it files what is already in
    the same way; unticking it leaves the documents alone - the lines of one
    already filed are a person's to correct, bar a credit, which becomes a
    return - but gives its products back
    (`importing.stop_expenses`), or the box could not be undone: flagged for
    ever, they reached no queue and no stock page, and correcting a document
    by hand resolved the very same flagged product.

    Said first, done once confirmed: it rewrites documents already filed,
    and a box on the Sources tab did it on a click. A POST without
    `confirme` - a page from before - shows what it would do, and does
    nothing."""
    from inventory.models import Product

    from .importing import charge_credits, redo_as_expenses, stop_expenses
    from .models import SupplierChange

    supplier = get_object_or_404(Supplier, pk=pk)
    fiche = reverse("invoices:supplier_detail", args=[supplier.pk])
    if request.method == "POST":
        wanted = bool(request.POST.get("expenses_only"))
    else:
        wanted = not supplier.expenses_only
    if request.method != "POST" or request.POST.get("confirme") != "1":
        documents = Invoice.objects.filter(supplier=supplier)
        return render(
            request,
            "invoices/supplier_expenses_confirm.html",
            {
                "supplier": supplier,
                "wanted": wanted,
                "unchanged": wanted == supplier.expenses_only,
                "document_count": documents.count(),
                "checked_count": documents.filter(reviewed_at__isnull=False).count(),
                "to_classify": Product.objects.filter(supplier=supplier, stock_type__isnull=True, is_expense=False).count(),
                "postes": Product.objects.filter(supplier=supplier, is_expense=True).count(),
                # Turned into returns on the way back to goods (stop_expenses).
                "credits": charge_credits(supplier).count(),
                "fiche": fiche,
            },
        )
    if wanted == supplier.expenses_only:
        messages.info(request, f"{supplier.name} est déjà un fournisseur de {'charges' if wanted else 'produits'}.")
        return redirect(fiche)
    with supplier_changes.cause(
        f"« {'Charges' if wanted else 'Produits'} » confirmé sur la fiche de {supplier.name}", by_person=True
    ):
        supplier.expenses_only = wanted
        supplier.save(update_fields=["expenses_only"])
        if not supplier.expenses_only:
            freed = stop_expenses(supplier)
            said = (
                f"{supplier.name} redevient un fournisseur de produits. Les documents déjà enregistrés gardent "
                "leurs lignes : corrigez-les document par document si besoin."
                + (f" {freed} poste(s) repassent à classer." if freed else "")
            )
        else:
            done = redo_as_expenses(supplier)
            said = (
                f"{supplier.name} : ses documents sont des charges - une ligne par taux de TVA, aucun produit à classer"
                + (f" ({done} document(s) déjà enregistré(s) refaits ainsi)." if done else ".")
            )
        supplier_changes.record(
            supplier, SupplierChange.Kind.CHARGES, said, data={"expenses_only": supplier.expenses_only}
        )
    messages.success(request, said)
    return redirect(fiche)


def receipt_queue(request):
    """"Achats", on what waits to be checked: the tickets, as a wall of
    thumbnails, and the documents to fix."""
    return render_purchases(request, "a-verifier")


def receipt_review(request, pk):
    """Check one receipt against its photo, then move to the next."""
    invoice = get_object_or_404(Invoice.objects.select_related("supplier"), pk=pk)
    if not invoice.is_receipt:
        return redirect("invoices:invoice_edit_lines", pk=invoice.pk)
    return _correction_page(request, invoice)


def _correction_page(request, invoice):
    """One page to correct a document against its photo or PDF: its date,
    its total and each of its lines - for a ticket and a supplier invoice
    alike.

    The photo, the checks and the editable lines are on one screen because
    they are one question - "does this say what the document says?" - and
    answering it by flipping between pages is what stops receipts being
    checked at all. The lines are checked against the total as they are
    typed (the page's script), and a ticket's stored checks are brought up to
    date when it is validated (receipts.recheck_after_review).

    Saving reuses `replace_invoice_lines`, the same path as a hand-typed
    invoice: there is no second way for lines to reach the database. A ticket
    still to check moves on to the next one; anything else goes back to its
    page.
    """
    is_receipt = invoice.is_receipt
    document = DOCUMENT_RECEIPT if is_receipt else DOCUMENT_INVOICE
    # Checking an import's tickets (`?lot=`) goes through them only, then
    # back to the import; the whole queue ends on what was checked.
    lot = _lot_of(request) if is_receipt else None
    pending = _pending_receipts()
    if lot is not None:
        pending = pending.filter(pk__in=batch_invoice_ids(lot))
    queue = list(pending.values_list("pk", flat=True)) if is_receipt else []
    next_pk = next((candidate for candidate in queue if candidate != invoice.pk), None)
    lot_query = f"?lot={lot.pk}" if lot is not None else ""
    here = redirect(request.get_full_path())

    price_form = ShopItemPriceForm()
    header = {"invoice_date": invoice.invoice_date, "printed_total_ttc": invoice.printed_total_ttc}
    header_form = DocumentHeaderForm(initial=header)
    vat_initial = _vat_initial(invoice)
    vat_form = (VatTableFormSet if vat_initial else EmptyVatTableFormSet)(prefix="tva", initial=vat_initial)
    formset = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "reread":
            _reread_from_page(request, invoice)
            return here

        if is_receipt and action == "unverify":
            invoice.reviewed_at = None
            invoice.save(update_fields=["reviewed_at"])
            messages.success(request, "Ticket remis dans la liste des tickets à vérifier.")
            return here

        if is_receipt and action == "rename_product":
            _rename_product_from_review(request, invoice)
            return here

        if is_receipt and action == "forget_price":
            _forget_price(request, invoice)
            return here

        if action == "move_shop":
            _move_shop(request, invoice)
            return here

        if is_receipt and action == "shop_header":
            _set_shop_header(request, invoice)
            return here

        if is_receipt and action == "remember_price":
            price_form = ShopItemPriceForm(request.POST, supplier=invoice.supplier)
            if price_form.is_valid():
                from .receipts import apply_known_prices

                price = price_form.save(commit=False)
                price.supplier = invoice.supplier
                price.save()
                applied = apply_known_prices(invoice)
                messages.success(
                    request,
                    f"Prix retenu : {price.unit_price_ttc} € = {price.label}"
                    + (
                        f" ({applied.lines} ligne(s) renommée(s) sur {applied.receipts} ticket(s))."
                        if applied.lines
                        else " (aucune ligne à renommer sur les tickets à vérifier)."
                    ),
                )
                return here
        elif action is None:
            # A charge takes a credit line at a count of 1 (LineCorrectionForm.clean)
            # - not where its supplier still has stock: a product a stock item
            # claimed stays one under charges (redo_as_expenses), a line on it
            # books a movement, and a credit there was stock at a negative
            # unit cost. The supplier's rather than the stored line's: which
            # product a row lands on is only known once saved, by its name
            # (replace_invoice_lines), and a row added or renamed onto the
            # stock item is that stock item.
            from inventory.models import Product

            charge = (
                invoice.supplier.expenses_only
                and not Product.objects.filter(supplier=invoice.supplier, stock_type__isnull=False).exists()
            )
            formset = LineCorrectionFormSet(request.POST, form_kwargs={"document": document, "charge": charge})
            header_form = DocumentHeaderForm(request.POST, initial=header)
            # Posted without the block (a page cached before it existed, a
            # request written by hand): the table is left as it was rather
            # than the whole correction being refused.
            vat_form = VatTableFormSet(request.POST, prefix="tva") if "tva-TOTAL_FORMS" in request.POST else None
            if formset.is_valid() and header_form.is_valid() and (vat_form is None or vat_form.is_valid()):
                was_pending = invoice.reviewed_at is None
                if _save_corrections(request, invoice, formset, header_form, vat_form):
                    if not is_receipt:
                        messages.success(request, f"{invoice.lines.count()} ligne(s) enregistrée(s).")
                        return redirect("invoices:invoice_detail", pk=invoice.pk)
                    messages.success(request, f"Ticket vérifié : {invoice}")
                    _say_where_products_are_renamed(request, invoice)
                    if not was_pending:
                        return redirect("invoices:invoice_detail", pk=invoice.pk)
                    if next_pk:
                        return redirect(reverse("invoices:receipt_review", args=[next_pk]) + lot_query)
                    if lot is not None:
                        return redirect("invoices:receipt_batch", pk=lot.pk)
                    return redirect(reverse("invoices:invoice_list") + "?filtre=verifies")

    if formset is None:
        formset = _line_formset_for(invoice, document)
    from .receipts import shop_choices

    shop_context = {"shop_groups": shop_choices(), "charges_default": invoice.supplier.expenses_only}
    if is_receipt:
        from .parsers import ticket_parser_for
        from .receipts import (
            PLACEHOLDER_MARKER,
            header_choices,
            header_guess,
            names_shop,
            offerable_headers,
            parser_for,
        )

        shop = getattr(parser_for(invoice.supplier), "shop", None)
        can_set_header = (
            ticket_parser_for(invoice.supplier.code) is None and invoice.supplier.parser_key != LLM_PARSER_KEY
        )
        shop_context |= {
            "suggested_header": header_guess(invoice.ocr_text),
            # Only what the save would take: the customer's own street is
            # printed on every supplier's documents.
            "header_choices": offerable_headers(
                header_choices(invoice.ocr_text), shop=invoice.supplier, ignoring=[invoice]
            )
            if can_set_header
            else [],
            "names_shop": names_shop(invoice.supplier),
            # A till configured here is known by its own layout, and nothing
            # is ever filed under the AI pseudo-supplier: no header to give.
            "can_set_header": can_set_header,
            # The shop's price list matters where its till prints no names, or
            # where one has been started: elsewhere it is folded away.
            "prices_open": bool(shop and shop.placeholder_names)
            or invoice.supplier.item_prices.exists()
            or invoice.lines.filter(raw_name__contains=PLACEHOLDER_MARKER).exists(),
        }
    return render(
        request,
        "invoices/document_review.html",
        {
            **_checks_context(invoice),
            "invoice": invoice,
            "vat_form": vat_form,
            "is_receipt": is_receipt,
            "is_invoice": not is_receipt,
            "formset": formset,
            "header_form": header_form,
            "price_form": price_form,
            "known_prices": invoice.supplier.item_prices.all() if is_receipt else [],
            "next_pk": next_pk,
            "remaining": len(queue),
            "lot": lot,
            "lot_query": lot_query,
            "can_reread": _can_reread(invoice),
            # An electronic invoice's file may be the XML itself, which no
            # frame can show: the page shows the document as text instead
            # (the reading written by einvoice._as_text_document).
            "is_einvoice": invoice.is_einvoice,
            # MINIMUM and BASIC WL state the totals and no line at all, so the
            # line below them was rebuilt from the VAT table (charge_reading)
            # and is NOT something the supplier declared. Said by the check
            # einvoice.NO_LINES_CHECK, which is where this reads it from -
            # the lead paragraph called every line the invoice's own data,
            # four lines above a check saying the invoice carries none.
            "einvoice_no_lines": invoice.is_einvoice and any(
                check["label"] == EINVOICE_NO_LINES for check in invoice.parse_checks
            ),
            # « Avoir » is a word this application never said out loud: a
            # credit note read as Factur-X was « Facture électronique » in the
            # list, in the header and in every check, and only the minus signs
            # gave it away. BT-112 comes out of the reader already signed, so
            # a negative stated total IS the credit note - no second field,
            # and true for one typed by hand too.
            "einvoice_is_credit": (
                invoice.is_einvoice
                and invoice.printed_total_ttc is not None
                and invoice.printed_total_ttc < 0
            ),
            "source_is_pdf": bool(invoice.source_file) and invoice.source_file.name.lower().endswith(".pdf"),
            "source_lines": invoice.source_text.split("\n") if invoice.is_einvoice else [],
            "ocr_lines": invoice.ocr_text.split("\n") if invoice.ocr_text else [],
            **shop_context,
        },
    )


def _lot_of(request):
    """The import a ticket is checked within, if the address names one."""
    posted = request.GET.get("lot", "")
    return ReceiptBatch.objects.filter(pk=posted).first() if is_id(posted) else None


def _save_corrections(request, invoice, formset, header_form, vat_form=None) -> bool:
    """Store what the page says. Returns whether it was saved."""
    from .receipts import _describe, learn_identifiers, recheck_after_review

    document = DOCUMENT_RECEIPT if invoice.is_receipt else DOCUMENT_INVOICE
    stored = {line.pk: line for line in invoice.lines.all()}
    lines = []
    for line_form in formset:
        if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
            continue
        line = stored.get(line_form.cleaned_data.get("line_id"))
        amounts = line_form.amounts(line)
        vat_rate = line_form.cleaned_data["vat_rate"] / Decimal("100")
        extra = {}
        if document == DOCUMENT_RECEIPT:
            extra = {
                "read_as": line_form.cleaned_data.get("read_as", ""),
                "printed_ttc": amounts["printed_ttc"],
                "discount_ttc": amounts["discount_ttc"],
                "discount": amounts["discount"],
            }
        elif line is not None and line_form.untouched(line):
            extra = {"printed_ttc": line.printed_ttc}
        lines.append(
            corrected_line(
                line,
                raw_name=line_form.cleaned_data["product_name"],
                quantity=line_form.cleaned_data["quantity"],
                total_ht=amounts["total_ht"],
                vat_rate=line.vat_rate if line is not None and line_form.untouched(line) else vat_rate,
                total_volume=line_form.volume(line),
                **extra,
            )
        )
    try:
        # One piece: a date saved on a document whose lines then failed to
        # save would be a change nobody validated.
        with transaction.atomic():
            invoice.invoice_date = header_form.cleaned_data["invoice_date"]
            # Left blank, the total read stays: a blank is a field nobody
            # filled in, not a value someone removed.
            if header_form.cleaned_data["printed_total_ttc"] is not None:
                invoice.printed_total_ttc = header_form.cleaned_data["printed_total_ttc"]
            replace_invoice_lines(invoice, lines)
            if invoice.supplier.expenses_only and document == DOCUMENT_RECEIPT:
                from .importing import charge_state

                # A charge's own checks and state are its total's, the one
                # typed here included: kept from the import, "Total de la
                # charge" said a total just typed was never read, and saved
                # with none, the charge came out settled.
                charge_state(invoice, invoice.printed_total_ttc)
            fields = ["invoice_date", "printed_total_ttc"]
            doubted = bool(invoice.supplier_doubt)
            if doubted:
                # Validated here, it is this supplier's: stored before the
                # learning below, which counts it among its documents now.
                invoice.supplier_doubt = ""
                invoice.save(update_fields=["supplier_doubt"])
                messages.info(request, f"Confirmé : ce document est bien de {invoice.supplier.name}.")
            if vat_form is not None:
                # The VAT table as the page now holds it, before the checks
                # that compare the lines against it are worked out - and it
                # is the person's, even empty: an emptied table was read
                # again from the photo, and checked against figures nobody
                # typed (receipts.vat_table).
                invoice.vat_breakdown = [form.row for form in vat_form if form.row is not None]
                invoice.vat_table_typed = True
                fields += ["vat_breakdown", "vat_table_typed"]
            if invoice.is_receipt:
                recheck_after_review(invoice)
                invoice.reviewed_at = timezone.now()
                fields += ["parse_checks", "reviewed_at"]
                # Checked, it is this shop's: its next tickets are
                # recognised by what this one prints, header or not.
                with supplier_changes.cause(
                    f"validation de {_describe(invoice)}", invoice=invoice, by_person=True
                ), supplier_changes.collect() as changes:
                    learn_identifiers(invoice.supplier, invoice.ocr_text)
                _say_supplier_changes(request, changes)
            elif doubted and invoice.source_text:
                # A digital invoice read by its supplier's own reader learns
                # at import - not this one, until now.
                with supplier_changes.cause(
                    f"validation de {_describe(invoice)}", invoice=invoice, by_person=True
                ), supplier_changes.collect() as changes:
                    learn_identifiers(invoice.supplier, invoice.source_text)
                _say_supplier_changes(request, changes)
            if invoice.is_receipt or doubted:
                _answer_first_document(request, invoice)
            invoice.save(update_fields=fields)
    except InvoiceLinesInUseError as exc:
        messages.error(request, str(exc))
        return False
    return True


def _answer_first_document(request, invoice) -> None:
    """Validated, a document is its supplier's - which is all that supplier's
    « premier document » asks (receipts._record_first_document): seen, not
    left lighting the « Enseignes et fournisseurs » tab. On 19/09 the owner
    validated Leroy Merlin's first document eight minutes after it was
    recorded, and the tab went on showing an amber « 1 » nobody could
    explain. Only the change of the supplier it is validated under: one
    moved here from another asks that other supplier to look at what it
    learned from a document that was not its own."""
    from .models import SupplierChange

    answered = SupplierChange.objects.filter(
        kind=SupplierChange.Kind.FIRST_DOCUMENT,
        supplier=invoice.supplier,
        invoice=invoice,
        reviewed_at__isnull=True,
        undone_at__isnull=True,
    ).update(reviewed_at=timezone.now())
    if answered:
        messages.info(request, f"Premier document de {invoice.supplier.name} : vu.")


def _vat_initial(invoice) -> list[dict]:
    """The VAT table as the page shows it: the rate in percent, as printed.

    At two decimals, the rate field's own (French rates have at most two in
    percent, receipt_base.read_rate): 0.055 x 100 is "5.500", which the page
    drew and its own field then refused when posted back - 361 documents
    could not be saved without retyping their 5,5 %. The base and tax stay
    as stored (the form takes four decimals)."""
    from .receipts import vat_table

    return [
        {"rate": (row["rate"] * Decimal("100")).quantize(Decimal("0.01")), "base": row["base"], "vat": row["vat"]}
        for row in vat_table(invoice)
    ]


def _checks_context(invoice) -> dict:
    """The checks beside the lines: the two kept up to date as they are
    typed - the lines against the total, and against the VAT table's HT
    base - and the rest as they were stored.

    The HT one used to be the stored one, and its label is one the reading
    writes too: the page marked it "(à la lecture du ticket)" on every
    document validated since the table was typed here, although validating
    rebuilds it from the lines as saved - 445 on 19/09, three tickets
    corrected by hand among them (two Leroy Merlin, read with each item's
    eco-participation as an item of its own) passing under that mark after
    their reading had failed it. And it never moved while the lines were
    typed. It is now worked out from the lines as they stand and the table
    the page shows, like the sum; with no table there is nothing to work it
    out from, and a stored one can only be the reading's (validating without
    a table drops it), so it stays, said as such, until a table is typed.

    A ticket's only, like the stored one (recheck_after_review): a supplier's
    invoice never showed it, and its lines leave out what the reconciliation
    adds (Invoice.reconciliation_adjustment, a duty) where the printed base
    counts it - a table typed there as printed failed it, "du ticket".
    """
    from .parsers.receipt_base import RECONCILIATION_TOLERANCE
    from .receipts import (
        HT_CHECK,
        READING_CHECKS,
        SUM_CHECK,
        lines_check,
        vat_table_checks,
    )

    ht_check = (
        next((check for check in vat_table_checks(invoice) if check["label"] == HT_CHECK), None)
        if invoice.is_receipt
        else None
    )
    live = {SUM_CHECK, HT_CHECK} if ht_check is not None else {SUM_CHECK}
    from .receipts import adjustment_counted

    return {
        "live_check": lines_check(invoice),
        "ht_check": ht_check,
        "ht_check_label": HT_CHECK,
        "other_checks": [check for check in invoice.parse_checks if check["label"] not in live],
        "tolerance": RECONCILIATION_TOLERANCE,
        # Duty charged globally, a document-level charge: part of the total
        # and of no line, so the script has to count it as the check does or
        # an invoice that balances turns red at the first keystroke.
        "live_adjustment": adjustment_counted(invoice),
        # What the parser said about lines a person may since have corrected:
        # shown as such, and replaced on validation.
        "reading_checks": READING_CHECKS,
    }


def _can_reread(invoice) -> bool:
    """Whether the page offers to read the document's file again."""
    from .receipts import parser_for

    if not invoice.source_file:
        return False
    if invoice.is_einvoice:
        # Its own file says everything again, exactly: there is no reader to
        # be missing, and no photograph to be read differently.
        return True
    if invoice.is_receipt:
        return parser_for(invoice.supplier) is not None
    parser = get_parser(invoice.supplier.parser_key)
    return parser is not None and invoice.supplier.parser_key != "LLM"


def _reread_from_page(request, invoice) -> None:
    from .receipts import RereadError, reread_document

    try:
        messages.success(request, reread_document(invoice))
    except (RereadError, InvoiceLinesInUseError) as exc:
        messages.error(request, str(exc))


def _say_supplier_changes(request, changes) -> None:
    """What an act changed in what recognises a supplier, said where it was
    done - a loss as a warning: on 18/09 one went without a word."""
    for change in changes:
        if change.kind != change.Kind.IDENTIFIERS:
            continue
        if change.data.get("lost"):
            messages.warning(request, change.summary)
        else:
            messages.info(request, change.summary)


def _move_shop(request, invoice) -> None:
    from .receipts import _describe, move_to_shop

    form = ReceiptShopForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.error_text())
        return
    if form.cleaned_data["supplier"] == invoice.supplier:
        kind = "ticket" if invoice.is_receipt else "document"
        messages.info(request, f"Ce {kind} est déjà rangé chez {invoice.supplier.name}.")
        return
    try:
        with supplier_changes.cause(
            f"changement de fournisseur de {_describe(invoice)}", invoice=invoice, by_person=True
        ), supplier_changes.collect() as changes:
            supplier, created = form.shop(ignoring=[invoice])
            move_to_shop(invoice, supplier)
    except (ValueError, InvoiceLinesInUseError) as exc:
        messages.error(request, str(exc))
        return
    kind = "Ticket" if invoice.is_receipt else "Facture"
    messages.success(request, f"{kind} rangé{'' if invoice.is_receipt else 'e'} chez {supplier.name}.")
    _say_supplier_changes(request, changes)
    if created:
        _say_new_shop(request, supplier)


def _set_shop_header(request, invoice) -> None:
    """The text this shop's documents print at the top, given from the page
    where one is on screen - the import card cannot ask for it, nobody has
    seen the document there yet."""
    from .receipt_batches import requeue_everywhere
    from .receipts import describe_tickets, set_shop_header, tickets_printing

    shop = invoice.supplier
    was = shop.ticket_header
    try:
        header = set_shop_header(shop, request.POST.get("ticket_header", "")[:100], ignoring=[invoice])
    except ValueError as exc:
        messages.error(request, str(exc))
        return
    if header == was:
        messages.info(request, f"{shop.name} garde le même en-tête : rien n'a changé.")
        _say_headerless(request, shop)
        return
    if not header:
        messages.success(
            request,
            f"{shop.name} n'est plus reconnue par un en-tête ; ses documents le seront par ce qu'ils impriment "
            "d'autre (n° SIREN, téléphone, site), quand ils en portent.",
        )
        return
    elsewhere = [ticket for ticket in tickets_printing(header, [invoice]) if ticket.supplier_id != shop.pk]
    if elsewhere:
        messages.warning(
            request,
            f"« {header} » est aussi imprimé sur {describe_tickets(elsewhere)} : si ce sont des documents de "
            f"{shop.name}, changez-les de fournisseur depuis la page de chacun.",
        )
    requeued = requeue_everywhere()
    messages.success(
        request,
        f"Les documents qui portent « {header} » iront chez {shop.name}"
        + (f" - {requeued} fichier(s) sans enseigne sont relus." if requeued else "."),
    )
    _say_headerless(request, shop)


def _say_headerless(request, shop) -> None:
    """A header only adds documents: those of `shop` that do not print it
    stay where something else they print put them. Said, with what that
    was, since someone giving a header may expect the others to leave."""
    from .identifiers import describe as describe_identifier
    from .identifiers import document_identifiers
    from .receipts import headerless_documents

    headerless = headerless_documents(shop)
    if not headerless:
        return
    total = Invoice.objects.filter(supplier=shop).count()
    learned = set(shop.ticket_identifiers or ())
    named_by = sorted({identifier for invoice in headerless for identifier in document_identifiers(invoice.document_text)} & learned)
    messages.info(
        request,
        f"{len(headerless)} des {total} documents {shop.name} ne portent pas « {shop.ticket_header} » : ils restent "
        f"chez {shop.name}"
        + (f", rangés par son {', son '.join(describe_identifier(i) for i in named_by)}" if named_by else "")
        + f". Si l'un d'eux n'est pas de {shop.name}, changez-le de fournisseur depuis sa page.",
    )


def _forget_price(request, invoice) -> None:
    """Drop one of the shop's known prices. Lines it already named keep their
    name: it is theirs now, and correcting one is done in the line."""
    from .models import ShopItemPrice

    posted = request.POST.get("price", "")
    price = ShopItemPrice.objects.filter(supplier=invoice.supplier, pk=posted).first() if is_id(posted) else None
    if price is None:
        messages.error(request, "Ce prix n'est pas (ou plus) connu pour cette enseigne.")
        return
    price.delete()
    since = f" (depuis le {price.valid_from:%d/%m/%Y})" if price.valid_from else ""
    messages.success(
        request,
        f"Prix oublié : {price.unit_price_ttc} € = {price.label}{since}. "
        "Les lignes déjà nommées ainsi gardent leur nom.",
    )


def _rename_product_from_review(request, invoice):
    """The "Renommer ... sur tous les tickets" form under a line. Only a
    product on this ticket: the screen offers nothing else."""
    from .receipts import rename_product

    product_id = request.POST.get("product", "")
    line = (
        invoice.lines.select_related("product").filter(product_id=product_id).first()
        if is_id(product_id)
        else None
    )
    if line is None:
        messages.error(request, "Ce produit n'est pas sur ce ticket.")
        return
    product, old = line.product, line.product.raw_name
    try:
        rename_product(product, request.POST.get("name", ""))
    except ValueError as exc:
        messages.error(request, str(exc))
        return
    messages.success(
        request,
        f"Produit renommé sur tous les tickets {invoice.supplier.name} : « {old} » devient « {product.raw_name} ».",
    )


def _say_where_products_are_renamed(request, invoice):
    """A name typed in a line that the matcher attached to a product of
    another spelling relabels that line only - the next ticket still shows
    the product's own name. Said once, where it can be acted on - and only
    where renaming is offered (see _line_formset_for)."""
    from .parsers import is_ticket_shop
    from .receipts import PLACEHOLDER_MARKER

    if not is_ticket_shop(invoice.supplier):
        return
    said = set()
    for line in invoice.lines.select_related("product"):
        product_name = line.product.raw_name
        if (
            line.raw_name != line.read_as
            and line.raw_name.casefold() != product_name.casefold()
            and PLACEHOLDER_MARKER not in product_name
            and (line.raw_name, product_name) not in said
        ):
            said.add((line.raw_name, product_name))
            messages.warning(
                request,
                f"« {line.raw_name} » est rattaché au produit « {product_name} », qui garde son nom sur les "
                "autres tickets. Pour le corriger partout : « Renommer … sur tous les tickets », sous sa ligne.",
            )


def _line_formset_for(invoice, document):
    """The correction form, pre-filled from the saved lines (line_initial).

    On a ticket, a line still named as OCR read it (`read_as`) but attached to
    a product of another spelling - by inventory.matching - is pre-filled with
    the product's own name, and the reading is shown under the row: the match
    is only safe because a person sees it here. Every other line keeps its own
    name: a line renamed from the shop's price list reads "Citron vert" while
    its product may still be the "Article divers" placeholder, and
    pre-filling the product's name would undo the rename. The reading rides
    along in a hidden field, so saving the page keeps it.
    """
    from .parsers import is_ticket_shop
    from .receipts import PLACEHOLDER_MARKER

    # Renaming is for the products of shops: a paper Metro ticket filed by
    # hand shows Metro's catalogue products, which its digital invoices find
    # by their exact name.
    can_rename = document == DOCUMENT_RECEIPT and is_ticket_shop(invoice.supplier)
    initial = []
    renamable = []
    offered = set()
    for line in invoice.lines.select_related("product"):
        row = line_initial(line, document)
        if document == DOCUMENT_RECEIPT and line.read_as and line.raw_name == line.read_as:
            row["product_name"] = line.product.raw_name
        # Offered under the first line of each product (six baguettes, one
        # rename), filled in with what the row says: the product's own name,
        # or the spelling someone typed that still landed on the old one -
        # the case the warning after a save points here for. Never on a
        # placeholder about to go.
        if can_rename and PLACEHOLDER_MARKER not in line.product.raw_name and line.product_id not in offered:
            offered.add(line.product_id)
            renamable.append((line.product, row["product_name"]))
        else:
            renamable.append((None, ""))
        initial.append(row)
    formset = LineCorrectionFormSet(initial=initial, form_kwargs={"document": document})
    for form, (product, rename_to) in zip(formset.forms, renamable):
        form.renamable_product = product
        form.rename_to = rename_to
    return formset


# --------------------------------------------------------------------------
# Deleting invoices - see invoices/deletion.py for what goes with them, and
# why an invoice that priced a stock take is kept.
# --------------------------------------------------------------------------


def _safe_next(request, default="invoices:invoice_list"):
    """Back to the page that asked (the receipt queue, say) - but never to
    another site: `next` comes from the request, so anyone can write it."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse(default)


def _deletion_entry(invoice, blockers):
    return {"invoice": invoice, "blockers": blockers, "line_count": invoice.lines.count()}


def _deletion_message(summaries):
    text = (
        f"Facture supprimée : {summaries[0].label}."
        if len(summaries) == 1
        else f"{len(summaries)} factures supprimées."
    )
    removed = sum(summary.products_removed for summary in summaries)
    if removed:
        text += f" {removed} produit(s) non classé(s) qui n'existai(en)t que par elle(s) retiré(s)."
    return text


def _render_delete_confirmation(request, entries, next_url, form_action):
    return render(
        request,
        "invoices/invoice_confirm_delete.html",
        {
            "entries": entries,
            "deletable_count": sum(1 for entry in entries if not entry["blockers"]),
            "next": next_url,
            "form_action": form_action,
        },
    )


def invoice_delete(request, pk):
    """One invoice. A GET only ever shows what would go; the POST deletes."""
    invoice = get_object_or_404(Invoice.objects.select_related("supplier"), pk=pk)
    next_url = _safe_next(request)
    blockers = blocking_stock_takes(invoice)
    if request.method == "POST":
        if not blockers:
            messages.success(request, _deletion_message([delete_invoice(invoice)]))
            return redirect(next_url)
        messages.error(request, str(InvoiceInUseError(invoice, blockers)))
    return _render_delete_confirmation(
        request,
        [_deletion_entry(invoice, blockers)],
        next_url,
        reverse("invoices:invoice_delete", args=[invoice.pk]),
    )


def invoice_bulk_delete(request):
    """Several invoices, ticked on a list page.

    Two POSTs: the first - straight from the checkboxes - only shows what
    would be deleted; the second, carrying `confirm`, does it. A tick box is
    too easy to leave checked under a search filter for it to delete on its
    own. An invoice that priced a stock take is kept and named, and doesn't
    stop the rest.
    """
    next_url = _safe_next(request)
    if request.method != "POST":
        return redirect(next_url)
    ids = [int(value) for value in request.POST.getlist("invoice_ids") if is_id(value)]
    invoices = list(Invoice.objects.filter(pk__in=ids).select_related("supplier").order_by("invoice_date", "pk"))
    if not invoices:
        messages.warning(request, "Aucune facture sélectionnée.")
        return redirect(next_url)

    entries = [_deletion_entry(invoice, blocking_stock_takes(invoice)) for invoice in invoices]
    if request.POST.get("confirm") != "1":
        return _render_delete_confirmation(request, entries, next_url, reverse("invoices:invoice_bulk_delete"))

    summaries = []
    for entry in entries:
        try:
            summaries.append(delete_invoice(entry["invoice"]))
        except InvoiceInUseError as exc:
            messages.error(request, str(exc))
    if summaries:
        messages.success(request, _deletion_message(summaries))
    return redirect(next_url)
