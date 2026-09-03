import os
import tempfile
import threading
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView

from .forms import EmailInvoiceSourceForm, InvoiceTypeForm, InvoiceUploadForm, ManualInvoiceForm, ManualInvoiceLineFormSet
from .importing import (
    DuplicateInvoiceError,
    import_parsed_invoice,
    parse_and_import,
    replace_invoice_lines,
)
from .models import Invoice, InvoiceType, ScrapeJob, Supplier
from .parsers.base import ParsedInvoice, ParsedLine
from .tasks import gather_invoices_task, suggested_start_date, test_email_pattern_task


class InvoiceListView(ListView):
    model = Invoice
    template_name = "invoices/invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        return Invoice.objects.select_related("supplier").all()

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

        scrapable_codes = set(Supplier.objects.filter(is_scrapable=True).values_list("code", flat=True))
        scrapable_codes |= {it.supplier.code for it in email_types}
        starts = [suggested_start_date(code) for code in scrapable_codes]
        context["default_start_date"] = min(starts) if starts else timezone.localdate()
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
                "vat_rate": line.vat_rate * Decimal("100"),
            }
            for line in invoice.lines.all()
        ]
        formset = ManualInvoiceLineFormSet(initial=initial)

    return render(
        request,
        "invoices/invoice_lines_form.html",
        {"invoice": invoice, "formset": formset},
    )
