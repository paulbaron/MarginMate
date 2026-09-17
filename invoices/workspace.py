"""The "Achats" page: invoices, tickets and invoice types in one place.

It used to be four pages - the invoice list, the ticket import, the tickets
to check and the invoice types - and adding a purchase then checking it had
landed meant going round them. Now one card on top adds purchases (ticket
photos, a supplier's PDF, a gather) and shows the import as it runs, and
three tabs below it list every document, what waits to be checked, and where
invoices come from. Every one of the old addresses draws this page, on the
tab or the import it was about (views.invoice_list, receipt_upload,
receipt_batch, receipt_queue, invoice_type_list...).
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .forms import InvoiceUploadForm, ReceiptBatchUploadForm
from .models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier
from .tasks import default_gather_start

#: "Ajoutés récemment", "Vérifiés récemment": what an import or a checking
#: session has just done, to see it landed.
RECENT = timedelta(hours=48)
IMPORT_TABS = ("tickets", "pdf", "recuperer")

IS_TICKET = ~Q(parse_checks=[]) | ~Q(ocr_text="")
IS_INVOICE = Q(parse_checks=[], ocr_text="")
#: The review queue (receipts.pending_receipts).
TICKET_TO_CHECK = Q(reviewed_at__isnull=True) & ~Q(parse_checks=[])
#: A document outside that queue that cannot be used as it is: undated (out
#: of every valuation and of the bank match) or failed to import.
DOCUMENT_TO_FIX = Q(parse_checks=[]) & (Q(invoice_date__isnull=True) | Q(status=Invoice.Status.ERROR))


def waiting_counts() -> dict:
    """What the "À vérifier" tab holds, in one query - it is on every page,
    through the navigation's count."""
    return Invoice.objects.aggregate(
        tickets=Count("pk", filter=TICKET_TO_CHECK),
        to_fix=Count("pk", filter=DOCUMENT_TO_FIX),
    )


def batch_invoice_ids(batch) -> list[int]:
    return [entry["invoice_id"] for entry in batch.results if entry.get("status") == "ok" and entry.get("invoice_id")]


def first_ticket_to_check(ids) -> tuple[int | None, int]:
    """The first of these tickets still to check, oldest first, and how many
    are left - where checking an import starts."""
    from .receipts import pending_receipts

    waiting = list(pending_receipts().filter(pk__in=ids).order_by("invoice_date", "id").values_list("pk", flat=True))
    return (waiting[0] if waiting else None), len(waiting)


def render_purchases(request, tab, *, status=200, **card):
    """The page, on `tab` ("documents", "a-verifier", "sources"), with the
    import card as `card` says (import_tab, batch, receipt_form, pdf_form)."""
    counts = Invoice.objects.aggregate(
        total=Count("pk"),
        tickets=Count("pk", filter=TICKET_TO_CHECK),
        to_fix=Count("pk", filter=DOCUMENT_TO_FIX),
    )
    waiting = counts["tickets"] + counts["to_fix"]
    context = {
        "tab": tab,
        "tabs": [
            {"key": "documents", "label": "Documents", "url": reverse("invoices:invoice_list"),
             "count": counts["total"], "attention": False},
            {"key": "a-verifier", "label": "À vérifier", "url": reverse("invoices:receipt_queue"),
             "count": waiting, "attention": bool(waiting)},
            {"key": "sources", "label": "Sources", "url": reverse("invoices:invoice_type_list"),
             "count": InvoiceType.objects.count(), "attention": False},
        ],
        **_import_card(request, **card),
    }
    for entry in context["tabs"]:
        entry["active"] = entry["key"] == tab
    if tab == "documents":
        context.update(_documents(request, card.get("batch")))
    elif tab == "a-verifier":
        context.update(_to_check())
    else:
        context.update(_sources())
    return render(request, "invoices/purchases.html", context, status=status)


def _import_card(request, import_tab=None, batch=None, receipt_form=None, pdf_form=None) -> dict:
    from .receipts import invoice_supplier_choices

    metro = Supplier.objects.filter(code="METRO", is_scrapable=True).first()
    email_types = list(
        InvoiceType.objects.filter(is_active=True, source_kind=InvoiceType.SourceKind.EMAIL).select_related("supplier")
    )
    gather_sources = []
    if metro:
        gather_sources.append({"code": "METRO", "label": metro.name})
    gather_sources += [{"code": f"type-{it.id}", "label": it.name} for it in email_types]
    # From the newest invoice these sources have already brought in: a
    # gather is for what arrived since. The earliest of each source's
    # latest used to be taken instead - one supplier billing twice a
    # year sent every gather ten months back, through 3,800 emails.
    gathered = {it.supplier_id for it in email_types}
    if metro:
        gathered.add(metro.pk)
    latest_job = ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).first()

    recent_batches = list(ReceiptBatch.objects.all()[:5])
    shown = batch
    if shown is None and recent_batches:
        # An import still running, stopped halfway or with tickets to file
        # stays in sight; a finished one is in "Derniers imports".
        latest = recent_batches[0]
        if latest.is_active or latest.can_resume or latest.awaiting_shop_count:
            shown = latest

    if import_tab not in IMPORT_TABS:
        import_tab = request.GET.get("ajouter", "")
    if import_tab not in IMPORT_TABS:
        if shown is not None:
            import_tab = "tickets"
        elif latest_job is not None and latest_job.is_active:
            import_tab = "recuperer"
        else:
            import_tab = ""

    card = {
        "import_tab": import_tab,
        "receipt_form": receipt_form or ReceiptBatchUploadForm(),
        "pdf_form": pdf_form or InvoiceUploadForm(),
        "invoice_supplier_groups": invoice_supplier_choices(),
        "gather_sources": gather_sources,
        "default_start_date": default_gather_start(gathered),
        "default_end_date": timezone.localdate(),
        "latest_job": latest_job,
        "recent_batches": recent_batches,
        "batch": shown,
    }
    if shown is not None:
        card.update(batch_status_context(shown))
    return card


def batch_status_context(batch) -> dict:
    """What the live part of an import draws: the import, the shops a file
    no shop was recognised on can be filed under, and where checking its
    tickets starts."""
    from .receipts import shop_choices

    first, left = first_ticket_to_check(batch_invoice_ids(batch))
    return {
        "batch": batch,
        "shop_groups": shop_choices() if batch.awaiting_shop_count else [],
        "batch_first_to_check": first,
        "batch_to_check": left,
    }


FILTERS = {
    "factures": ("Factures", IS_INVOICE),
    "tickets": ("Tickets", IS_TICKET),
    "recents": ("Ajoutés récemment", None),
    "verifies": ("Vérifiés récemment", None),
    "sans-date": ("Sans date", Q(invoice_date__isnull=True)),
}


def _documents(request, batch) -> dict:
    since = timezone.now() - RECENT
    conditions = {
        **{key: condition for key, (_label, condition) in FILTERS.items() if condition is not None},
        "recents": Q(imported_at__gte=since),
        "verifies": Q(reviewed_at__gte=since),
    }
    counts = Invoice.objects.aggregate(
        total=Count("pk"), **{key.replace("-", "_"): Count("pk", filter=q) for key, q in conditions.items()}
    )
    invoices = Invoice.objects.select_related("supplier").prefetch_related("lines")
    active = request.GET.get("filtre", "")
    if request.GET.get("sans_date"):
        active = "sans-date"
    if batch is not None:
        active = ""
        invoices = invoices.filter(pk__in=batch_invoice_ids(batch))
    elif active in conditions:
        invoices = invoices.filter(conditions[active])
        if active == "verifies":
            invoices = invoices.order_by("-reviewed_at")
    else:
        active = ""

    chips = [{"key": "", "label": "Tous", "count": counts["total"]}]
    for key, (label, _condition) in FILTERS.items():
        count = counts[key.replace("-", "_")]
        # The kinds always; the others when they hold something.
        if key in ("factures", "tickets") or count or key == active:
            chips.append({"key": key, "label": label, "count": count})
    for chip in chips:
        chip["active"] = batch is None and chip["key"] == active
        chip["url"] = reverse("invoices:invoice_list") + (f"?filtre={chip['key']}" if chip["key"] else "")

    rows = list(invoices)
    posted = request.GET.get("surligner", "")
    highlight = int(posted) if posted.isdigit() else None
    if highlight is not None:
        # Stable: the highlighted document first, the rest in their order.
        rows.sort(key=lambda invoice: invoice.pk != highlight)
    return {
        "invoices": rows,
        "chips": chips,
        "active_filter": active,
        "highlight": highlight,
        "lot": batch,
        "undated_count": counts["sans_date"],
    }


def _to_check() -> dict:
    from .receipts import pending_receipts

    receipts = list(pending_receipts().select_related("supplier").prefetch_related("lines").order_by("invoice_date", "id"))
    return {
        "receipts": receipts,
        "verified_count": Invoice.objects.filter(reviewed_at__isnull=False).count(),
        "to_fix": list(Invoice.objects.filter(DOCUMENT_TO_FIX).select_related("supplier").prefetch_related("lines")),
        # Their products wait on the Produits page, not here.
        "with_products_to_classify": Invoice.objects.filter(IS_INVOICE, status=Invoice.Status.NEEDS_REVIEW).count(),
    }


def _sources() -> dict:
    from .parsers import is_ticket_shop

    return {
        "invoice_types": InvoiceType.objects.select_related("supplier", "email_source"),
        "ticket_shops": [supplier for supplier in Supplier.objects.all() if is_ticket_shop(supplier)],
    }
