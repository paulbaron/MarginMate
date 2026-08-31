import os
import tempfile
import threading
from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView

from .forms import InvoiceUploadForm
from .importing import DuplicateInvoiceError, parse_and_import
from .models import Invoice, ScrapeJob, Supplier
from .tasks import gather_invoices_task, suggested_start_date


class InvoiceListView(ListView):
    model = Invoice
    template_name = "invoices/invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        return Invoice.objects.select_related("supplier").all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["latest_job"] = ScrapeJob.objects.first()
        scrapable_codes = Supplier.objects.filter(is_scrapable=True).values_list("code", flat=True)
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

    active_job = ScrapeJob.objects.filter(status__in=[ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING]).first()
    if active_job is None:
        start_date = _parse_date(request.POST.get("start_date"))
        end_date = _parse_date(request.POST.get("end_date"))
        active_job = ScrapeJob.objects.create(range_start=start_date, range_end=end_date)
        thread = threading.Thread(
            target=gather_invoices_task, args=(active_job.id, start_date, end_date), daemon=True
        )
        thread.start()
    return redirect("invoices:invoice_list")


def gather_status(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id)
    return render(request, "invoices/_gather_status.html", {"job": job})
