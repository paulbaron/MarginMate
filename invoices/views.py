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
from django.views.generic import DetailView, ListView

from .deletion import InvoiceInUseError, blocking_stock_takes, delete_invoice
from .forms import (
    DOCUMENT_INVOICE,
    DOCUMENT_RECEIPT,
    DocumentHeaderForm,
    EmailInvoiceSourceForm,
    InvoiceTypeForm,
    InvoiceUploadForm,
    LineCorrectionFormSet,
    ManualInvoiceForm,
    ManualInvoiceLineFormSet,
    ReceiptBatchUploadForm,
    ReceiptShopForm,
    ShopItemPriceForm,
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
from .parsers import get_parser
from .parsers.base import ParsedInvoice, ParsedLine
from .tasks import default_gather_start, gather_invoices_task, test_email_pattern_task


class InvoiceListView(ListView):
    model = Invoice
    template_name = "invoices/invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        # Lines prefetched: every row shows totals added up from them.
        invoices = Invoice.objects.select_related("supplier").prefetch_related("lines")
        if self.request.GET.get("sans_date"):
            invoices = invoices.filter(invoice_date__isnull=True)
        return invoices

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["latest_job"] = ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).first()
        # A document with no date sits outside every stock valuation and the
        # bank match: counted, and one click from the list of them.
        context["undated_count"] = Invoice.objects.filter(invoice_date__isnull=True).count()
        context["undated_only"] = bool(self.request.GET.get("sans_date"))

        metro_supplier = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
        email_types = list(
            InvoiceType.objects.filter(is_active=True, source_kind=InvoiceType.SourceKind.EMAIL).select_related(
                "supplier"
            )
        )
        gather_sources = []
        if metro_supplier:
            gather_sources.append({"code": "METRO", "label": metro_supplier.name})
        gather_sources += [{"code": f"type-{it.id}", "label": it.name} for it in email_types]
        context["gather_sources"] = gather_sources

        # From the newest invoice these sources have already brought in: a
        # gather is for what arrived since. The earliest of each source's
        # latest used to be taken instead - one supplier billing twice a
        # year sent every gather ten months back, through 3,800 emails.
        gathered = {it.supplier_id for it in email_types}
        if metro_supplier:
            gathered.add(metro_supplier.pk)
        context["default_start_date"] = default_gather_start(gathered)
        context["default_end_date"] = timezone.localdate()
        return context


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
        return context


def upload_invoice(request):
    if request.method == "POST":
        form = InvoiceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            supplier = form.cleaned_data["supplier"]
            uploaded = form.cleaned_data["source_file"]
            suffix = os.path.splitext(uploaded.name)[1] or ".pdf"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as tmp:
                    for chunk in uploaded.chunks():
                        tmp.write(chunk)
                invoice = parse_and_import(tmp_path, supplier, display_filename=uploaded.name)
                messages.success(request, f"Facture importée : {invoice}")
                return redirect("invoices:invoice_detail", pk=invoice.pk)
            except DuplicateInvoiceError as exc:
                messages.warning(request, str(exc))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, not a crash
                messages.error(request, f"Échec de l'import : {exc}")
            finally:
                os.unlink(tmp_path)
    else:
        form = InvoiceUploadForm()
    return render(request, "invoices/invoice_upload.html", {"form": form})


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
    if active_job is None:
        start_date = _parse_date(request.POST.get("start_date"))
        end_date = _parse_date(request.POST.get("end_date"))
        source_codes = set(request.POST.getlist("sources"))
        active_job = ScrapeJob.objects.create(range_start=start_date, range_end=end_date)
        thread = threading.Thread(
            target=gather_invoices_task, args=(active_job.id, start_date, end_date, source_codes), daemon=True
        )
        thread.start()
    return redirect("invoices:invoice_list")


def gather_status(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id)
    return render(request, "invoices/_gather_status.html", {"job": job})


def cancel_gather(request, job_id):
    if request.method != "POST":
        return redirect("invoices:invoice_list")
    job = get_object_or_404(ScrapeJob, pk=job_id)
    if job.status in (ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING):
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
    # Renders the same partial gather_status does (rather than redirecting)
    # so the htmx-powered "Annuler" button can swap it in directly, whether
    # the job being cancelled is a real gather (invoice_list.html) or a
    # pattern test (invoice_type_form.html) - both already include this
    # same partial for their live status card.
    return render(request, "invoices/_gather_status.html", {"job": job})


class InvoiceTypeListView(ListView):
    model = InvoiceType
    template_name = "invoices/invoice_type_list.html"
    context_object_name = "invoice_types"

    def get_queryset(self):
        return InvoiceType.objects.select_related("supplier", "email_source")


def invoice_type_form(request, pk=None):
    invoice_type = get_object_or_404(InvoiceType, pk=pk) if pk else None
    source = getattr(invoice_type, "email_source", None) if invoice_type else None
    test_job = None

    if request.method == "POST":
        type_form = InvoiceTypeForm(request.POST, instance=invoice_type)
        source_form = EmailInvoiceSourceForm(request.POST, instance=source)

        if request.POST.get("action") == "test":
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
                saved_type = type_form.save(commit=False)
                saved_type.source_kind = InvoiceType.SourceKind.EMAIL
                saved_type.save()
                saved_source = source_form.save(commit=False)
                saved_source.invoice_type = saved_type
                saved_source.save()
                messages.success(request, f"Type de facture enregistré : {saved_type.name}")
                return redirect("invoices:invoice_type_list")
    else:
        type_form = InvoiceTypeForm(instance=invoice_type)
        source_form = EmailInvoiceSourceForm(
            instance=source,
            initial={
                "test_start_date": timezone.localdate() - timedelta(days=30),
                "test_end_date": timezone.localdate(),
            },
        )

    return render(
        request,
        "invoices/invoice_type_form.html",
        {
            "type_form": type_form,
            "source_form": source_form,
            "test_job": test_job,
            "invoice_type": invoice_type,
            # Rendered by hand beside the "Tester" button rather than among
            # the pattern fields, so the shared partial leaves them out.
            "test_date_fields": ["test_start_date", "test_end_date"],
        },
    )


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
    """Receipt photos in: a few files, or a whole folder.

    The files are staged and handed to a background job
    (invoices/receipt_batches.py) - a folder is minutes of OCR, far too long
    to hold a request open - and the browser goes straight to that batch's
    page, which fills in file by file.
    """
    from .receipt_batches import stage_batch, start_batch

    ReceiptBatch.reap_stale()
    if request.method == "POST":
        form = ReceiptBatchUploadForm(request.POST, request.FILES)
        if form.is_valid():
            batch = stage_batch(form.cleaned_data["files"], form.ignored_names)
            start_batch(batch)
            return redirect("invoices:receipt_batch", pk=batch.pk)
    else:
        form = ReceiptBatchUploadForm()

    return render(
        request,
        "invoices/receipt_upload.html",
        {"form": form, "batches": ReceiptBatch.objects.all()[:5], "pending_count": _pending_receipts().count()},
    )


def _batch_status_context(batch):
    """What the live part of a batch page draws: the batch, and the shops a
    file no shop was recognised on can be filed under."""
    from .receipts import shop_choices

    return {"batch": batch, "shop_groups": shop_choices() if batch.awaiting_shop_count else []}


def receipt_batch(request, pk):
    """One import: its progress while it runs, every file's outcome after."""
    ReceiptBatch.reap_stale()
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    return render(
        request,
        "invoices/receipt_batch.html",
        {**_batch_status_context(batch), "pending_count": _pending_receipts().count()},
    )


def receipt_batch_status(request, pk):
    """The live part of the batch page, re-fetched by htmx every second while
    the batch runs. It is all that changes on screen, so it has to notice a
    dead batch itself - or "En cours" stays up for ever."""
    ReceiptBatch.reap_stale()
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    return render(request, "invoices/_receipt_batch_status.html", _batch_status_context(batch))


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
        messages.error(request, " ".join(form.errors["supplier"]))
        return redirect("invoices:receipt_batch", pk=batch.pk)
    supplier = form.cleaned_data["supplier"]
    try:
        entry = import_with_shop(batch, index, supplier)
    except ShopChoiceError as exc:
        messages.error(request, str(exc))
        return redirect("invoices:receipt_batch", pk=batch.pk)
    if entry["status"] != "ok":
        messages.warning(request, entry["message"])
        return redirect("invoices:receipt_batch", pk=batch.pk)
    messages.success(request, f"{entry['name']} importé comme ticket {supplier.name} : vérifiez-le d'après la photo.")
    return redirect("invoices:receipt_review", pk=entry["invoice_id"])


def receipt_batch_cancel(request, pk):
    """Stop after the file being read now. What was already imported stays."""
    if request.method != "POST":
        return redirect("invoices:receipt_batch", pk=pk)
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    if batch.is_active:
        batch.cancel_requested = True
        batch.save(update_fields=["cancel_requested"])
    return render(request, "invoices/_receipt_batch_status.html", _batch_status_context(batch))


def receipt_queue(request):
    """Everything waiting to be checked, as a wall of thumbnails."""
    # Lines prefetched: every card shows a total added up from them.
    pending = list(_pending_receipts().prefetch_related("lines"))
    return render(
        request,
        "invoices/receipt_queue.html",
        {
            "receipts": pending,
            "verified_count": Invoice.objects.filter(reviewed_at__isnull=False).count(),
        },
    )


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
    queue = list(_pending_receipts().values_list("pk", flat=True)) if is_receipt else []
    next_pk = next((candidate for candidate in queue if candidate != invoice.pk), None)
    here = redirect(request.path)

    price_form = ShopItemPriceForm()
    header = {"invoice_date": invoice.invoice_date, "printed_total_ttc": invoice.printed_total_ttc}
    header_form = DocumentHeaderForm(initial=header)
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
            formset = LineCorrectionFormSet(request.POST, form_kwargs={"document": document})
            header_form = DocumentHeaderForm(request.POST, initial=header)
            if formset.is_valid() and header_form.is_valid():
                was_pending = invoice.reviewed_at is None
                if _save_corrections(request, invoice, formset, header_form):
                    if not is_receipt:
                        messages.success(request, f"{invoice.lines.count()} ligne(s) enregistrée(s).")
                        return redirect("invoices:invoice_detail", pk=invoice.pk)
                    messages.success(request, f"Ticket vérifié : {invoice}")
                    _say_where_products_are_renamed(request, invoice)
                    if not was_pending:
                        return redirect("invoices:invoice_detail", pk=invoice.pk)
                    if next_pk:
                        return redirect("invoices:receipt_review", pk=next_pk)
                    return redirect("invoices:receipt_queue")

    if formset is None:
        formset = _line_formset_for(invoice, document)
    return render(
        request,
        "invoices/document_review.html",
        {
            **_checks_context(invoice),
            "invoice": invoice,
            "is_receipt": is_receipt,
            "formset": formset,
            "header_form": header_form,
            "price_form": price_form,
            "known_prices": invoice.supplier.item_prices.all() if is_receipt else [],
            "next_pk": next_pk,
            "remaining": len(queue),
            "can_reread": _can_reread(invoice),
            "source_is_pdf": bool(invoice.source_file) and invoice.source_file.name.lower().endswith(".pdf"),
            "ocr_lines": invoice.ocr_text.split("\n") if invoice.ocr_text else [],
        },
    )


def _save_corrections(request, invoice, formset, header_form) -> bool:
    """Store what the page says. Returns whether it was saved."""
    from .receipts import recheck_after_review

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
            fields = ["invoice_date", "printed_total_ttc"]
            if invoice.is_receipt:
                recheck_after_review(invoice)
                invoice.reviewed_at = timezone.now()
                fields += ["parse_checks", "reviewed_at"]
            invoice.save(update_fields=fields)
    except InvoiceLinesInUseError as exc:
        messages.error(request, str(exc))
        return False
    return True


def _checks_context(invoice) -> dict:
    """The checks beside the lines: the one kept up to date as they are
    typed, and the rest as they were stored."""
    from .parsers.receipt_base import RECONCILIATION_TOLERANCE
    from .receipts import READING_CHECKS, SUM_CHECK, lines_check

    return {
        "live_check": lines_check(invoice),
        "other_checks": [check for check in invoice.parse_checks if check["label"] != SUM_CHECK],
        "tolerance": RECONCILIATION_TOLERANCE,
        # What the parser said about lines a person may since have corrected:
        # shown as such, and replaced on validation.
        "reading_checks": READING_CHECKS,
    }


def _can_reread(invoice) -> bool:
    """Whether the page offers to read the document's file again."""
    from .receipts import parser_for

    if not invoice.source_file:
        return False
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


def _forget_price(request, invoice) -> None:
    """Drop one of the shop's known prices. Lines it already named keep their
    name: it is theirs now, and correcting one is done in the line."""
    from .models import ShopItemPrice

    price = ShopItemPrice.objects.filter(supplier=invoice.supplier, pk=request.POST.get("price") or 0).first()
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
        if product_id.isdigit()
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
    from .receipts import PLACEHOLDER_MARKER, parser_for

    if parser_for(invoice.supplier) is None:
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
    from .receipts import PLACEHOLDER_MARKER, parser_for

    # Renaming is for the products of shops whose tickets are read: a paper
    # Metro ticket filed by hand shows Metro's catalogue products, which its
    # digital invoices find by their exact name.
    can_rename = document == DOCUMENT_RECEIPT and parser_for(invoice.supplier) is not None
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
    ids = [int(value) for value in request.POST.getlist("invoice_ids") if value.isdigit()]
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
