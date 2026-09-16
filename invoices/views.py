import os
import tempfile
import threading
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import DetailView, ListView

from .forms import (
    EmailInvoiceSourceForm,
    InvoiceTypeForm,
    InvoiceUploadForm,
    ManualInvoiceForm,
    ManualInvoiceLineFormSet,
    ReceiptBatchUploadForm,
    ReceiptLineFormSet,
    ShopItemPriceForm,
)
from .importing import (
    DuplicateInvoiceError,
    import_parsed_invoice,
    parse_and_import,
    replace_invoice_lines,
)

from .deletion import InvoiceInUseError, blocking_stock_takes, delete_invoice
from .models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, ShopItemPrice, Supplier
from .parsers import get_parser
from .parsers.base import ParsedInvoice, ParsedLine
from .tasks import default_gather_start, gather_invoices_task, test_email_pattern_task


class InvoiceListView(ListView):
    model = Invoice
    template_name = "invoices/invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        # Lines prefetched: every row shows totals added up from them.
        return Invoice.objects.select_related("supplier").prefetch_related("lines")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["latest_job"] = ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).first()

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


def _vat_percent_for_form(line):
    """The line's VAT rate as the line-entry form will accept it back.

    `InvoiceLine.vat_rate` is stored to four decimals, so 5.5% is 0.0550 and
    multiplying by 100 gives "5.5000" - which the form's own
    DecimalField(decimal_places=2) then rejects. Rendering a value a page
    refuses on submit fails in the worst way available: the error lands under
    a field nobody touched, on a form the user has just spent time
    correcting.
    """
    return (line.vat_rate * Decimal("100")).quantize(Decimal("0.01"))


def edit_invoice_lines(request, pk):
    """Type an invoice's lines in by hand.

    For invoices that arrived with no parser (see importing.parse_and_import)
    and for correcting one that did. Reuses the manual-invoice line formset,
    so there's one way to enter a line rather than two that drift.

    Saving replaces the lines wholesale: an invoice is a document, and the
    lines are what it says. Editing them in place would mean reconciling
    which existing line each row refers to, and stock movements already
    created from them - deleting and recreating is both simpler and
    exactly what "this is what the invoice actually says" means.
    """
    invoice = get_object_or_404(Invoice.objects.select_related("supplier"), pk=pk)

    if request.method == "POST":
        formset = ManualInvoiceLineFormSet(request.POST)
        if formset.is_valid():
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
            replace_invoice_lines(invoice, lines)
            messages.success(request, f"{len(lines)} ligne(s) enregistrée(s).")
            return redirect("invoices:invoice_detail", pk=invoice.pk)
    else:
        initial = [
            {
                "product_name": line.raw_name,
                "quantity": line.quantity,
                "total_ht": line.total_ht,
                "vat_rate": _vat_percent_for_form(line),
            }
            for line in invoice.lines.all()
        ]
        formset = ManualInvoiceLineFormSet(initial=initial)

    return render(
        request,
        "invoices/invoice_lines_form.html",
        {
            "invoice": invoice,
            "formset": formset,
            "has_parser": get_parser(invoice.supplier.parser_key) is not None,
        },
    )


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
    return (
        Invoice.objects.filter(reviewed_at__isnull=True)
        .exclude(parse_checks=[])
        .select_related("supplier")
        .order_by("invoice_date", "id")
    )


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


def receipt_batch(request, pk):
    """One import: its progress while it runs, every file's outcome after."""
    ReceiptBatch.reap_stale()
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    return render(
        request, "invoices/receipt_batch.html", {"batch": batch, "pending_count": _pending_receipts().count()}
    )


def receipt_batch_status(request, pk):
    """The live part of the batch page, re-fetched by htmx every second while
    the batch runs."""
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    return render(request, "invoices/_receipt_batch_status.html", {"batch": batch})


def receipt_batch_cancel(request, pk):
    """Stop after the file being read now. What was already imported stays."""
    if request.method != "POST":
        return redirect("invoices:receipt_batch", pk=pk)
    batch = get_object_or_404(ReceiptBatch, pk=pk)
    if batch.is_active:
        batch.cancel_requested = True
        batch.save(update_fields=["cancel_requested"])
    return render(request, "invoices/_receipt_batch_status.html", {"batch": batch})


def receipt_queue(request):
    """Everything waiting to be checked, as a wall of thumbnails."""
    pending = list(_pending_receipts())
    return render(
        request,
        "invoices/receipt_queue.html",
        {
            "receipts": pending,
            "verified_count": Invoice.objects.filter(reviewed_at__isnull=False).count(),
        },
    )


def receipt_review(request, pk):
    """Check one receipt against its photo, then move to the next.

    The photo, the parser's checks and the editable lines are on one screen
    because they are one question - "does this say what the ticket says?" -
    and answering it by flipping between three pages is what stops receipts
    being checked at all.

    Saving reuses `replace_invoice_lines`, the same path as a hand-typed
    invoice, so a corrected receipt and a typed one end up identical - there
    is no second way for lines to reach the database.
    """
    invoice = get_object_or_404(Invoice.objects.select_related("supplier"), pk=pk)
    queue = list(_pending_receipts().values_list("pk", flat=True))
    next_pk = next((candidate for candidate in queue if candidate != invoice.pk), None)

    price_form = ShopItemPriceForm()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "remember_price":
            price_form = ShopItemPriceForm(request.POST)
            if price_form.is_valid():
                price = price_form.save(commit=False)
                price.supplier = invoice.supplier
                price.save()
                relabelled = _apply_price_to_invoice(invoice, price)
                messages.success(
                    request,
                    f"Prix retenu : {price.unit_price_ttc} € = {price.label}"
                    + (f" ({relabelled} ligne(s) renommée(s))." if relabelled else "."),
                )
                return redirect("invoices:receipt_review", pk=invoice.pk)
            formset = _line_formset_for(invoice)
        else:
            formset = ReceiptLineFormSet(request.POST)
            if formset.is_valid():
                lines = []
                for line_form in formset:
                    if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                        continue
                    quantity = line_form.cleaned_data["quantity"]
                    total_ht = line_form.cleaned_total_ht()
                    lines.append(
                        ParsedLine(
                            raw_name=line_form.cleaned_data["product_name"],
                            read_as=line_form.cleaned_data.get("read_as", ""),
                            quantity=quantity,
                            total_volume=Decimal("0"),
                            unit_cost_ht=(total_ht / quantity).quantize(Decimal("0.0001"))
                            if quantity
                            else Decimal("0"),
                            total_ht=total_ht,
                            vat_rate=line_form.cleaned_data["vat_rate"] / Decimal("100"),
                        )
                    )
                replace_invoice_lines(invoice, lines)
                invoice.reviewed_at = timezone.now()
                invoice.save(update_fields=["reviewed_at"])
                messages.success(request, f"Ticket vérifié : {invoice}")
                if next_pk:
                    return redirect("invoices:receipt_review", pk=next_pk)
                return redirect("invoices:receipt_queue")
    else:
        formset = _line_formset_for(invoice)

    lines = list(invoice.lines.select_related("product").all())
    return render(
        request,
        "invoices/receipt_review.html",
        {
            "invoice": invoice,
            "formset": formset,
            "price_form": price_form,
            "lines": lines,
            "known_prices": invoice.supplier.item_prices.all(),
            "next_pk": next_pk,
            "remaining": len(queue),
            "ocr_lines": invoice.ocr_text.split("\n") if invoice.ocr_text else [],
        },
    )


def _line_formset_for(invoice):
    """The review form, pre-filled from the saved lines.

    A line still named as OCR read it (`read_as`) but attached to a product
    of another spelling - by inventory.matching - is pre-filled with the
    product's own name, and the reading is shown under the row: the match is
    only safe because a person sees it here. Every other line keeps its own
    name: a line renamed from the shop's price list reads "Citron vert" while
    its product may still be the "Article divers" placeholder, and
    pre-filling the product's name would undo the rename. The reading rides
    along in a hidden field, so saving the page keeps it.
    """
    initial = []
    for line in invoice.lines.select_related("product"):
        name = line.raw_name
        if line.read_as and line.raw_name == line.read_as:
            name = line.product.raw_name
        initial.append(
            {
                "product_name": name,
                "read_as": line.read_as,
                "quantity": line.quantity,
                # As the ticket prints it; see ReceiptLineForm.total_ttc.
                "total_ttc": (line.total_ht * (Decimal("1") + line.vat_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                "vat_rate": _vat_percent_for_form(line),
            }
        )
    return ReceiptLineFormSet(initial=initial)


def _apply_price_to_invoice(invoice, price) -> int:
    """Rename this invoice's unnamed lines that match a newly recorded price.

    Only the placeholder ones, and only on this invoice: rewriting names
    across history would change what past invoices claim to have bought.
    """
    from .receipts import PLACEHOLDER_MARKER

    renamed = 0
    for line in invoice.lines.all():
        if PLACEHOLDER_MARKER not in line.raw_name:
            continue
        unit_ttc = (line.unit_cost_ht * (Decimal("1") + line.vat_rate)).quantize(Decimal("0.01"))
        if unit_ttc == price.unit_price_ttc:
            line.raw_name = price.label
            line.save(update_fields=["raw_name"])
            renamed += 1
    return renamed


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
