"""The "Achats" page: invoices, tickets, their sources and suppliers in one
place.

It used to be four pages - the invoice list, the ticket import, the tickets
to check and the invoice types - and adding a purchase then checking it had
landed meant going round them. Now one card on top adds purchases (ticket
photos, a supplier's PDF, a gather) and shows the import as it runs, and
four tabs below it list every document, what waits to be checked, where
invoices come from (« Sources »: the InvoiceTypes) and who each document is
filed under (« Enseignes et fournisseurs »). Every one of the old addresses
draws this page, on the tab or the import it was about (views.invoice_list,
receipt_upload, receipt_batch, receipt_queue, invoice_type_list,
supplier_views.supplier_list...).
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from common import is_id

from .forms import CHANNELS, InvoiceUploadForm, ReceiptBatchUploadForm
from .models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier
from .tasks import default_gather_start

#: "Ajoutés récemment", "Vérifiés récemment": what an import or a checking
#: session has just done, to see it landed.
RECENT = timedelta(hours=48)
# One import for every document - a photo, a scan, a supplier's PDF: what
# each file is decides how it is read (receipts.import_document). "tickets"
# and "pdf" were two, and are kept as names of the same one.
IMPORT_TABS = ("documents", "recuperer")
OLD_IMPORT_TABS = {"tickets": "documents", "pdf": "documents"}

IS_TICKET = ~Q(parse_checks=[]) | ~Q(ocr_text="")
IS_INVOICE = Q(parse_checks=[], ocr_text="")
#: A supplier of charges: a rent, a subscription (Supplier.expenses_only).
IS_CHARGE = Q(supplier__expenses_only=True)
#: The review queue. `receipts.pending_receipts` is this Q and nothing else:
#: written twice, the tab counted the charges its own list left out, and
#: said "À vérifier 102" over an empty page.
TICKET_TO_CHECK = Q(reviewed_at__isnull=True) & ~Q(parse_checks=[]) & ~IS_CHARGE
#: A document outside that queue that cannot be used as it is: undated (out
#: of every valuation and of the bank match), failed to import, a charge
#: whose own total could not be read (importing.charge_needs_a_look), or one
#: an invoice type fetched whose supplier is in doubt (Invoice.supplier_doubt:
#: a ticket among them waits in the queue while it is unchecked) - it is in
#: no queue, so this is where it is seen.
DOCUMENT_TO_FIX = (
    (Q(parse_checks=[]) & (Q(invoice_date__isnull=True) | Q(status=Invoice.Status.ERROR)))
    | (IS_CHARGE & Q(status=Invoice.Status.NEEDS_REVIEW))
    | (~Q(supplier_doubt="") & ~TICKET_TO_CHECK)
)
#: The supplier « Récupérer les nouvelles factures » fetches with a module of
#: its own rather than through a source (tasks.gather_invoices_task): Metro.
#: UBA is seeded `is_scrapable` too, but a mailbox source fetches it.
OWN_MODULE = Q(code="METRO", is_scrapable=True)


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
    """The page, on `tab` ("documents", "a-verifier", "sources",
    "fournisseurs"), with the import card as `card` says (import_tab, batch,
    receipt_form, pdf_form)."""
    from .parsers import LLM_PARSER_KEY

    counts = Invoice.objects.aggregate(
        total=Count("pk"),
        tickets=Count("pk", filter=TICKET_TO_CHECK),
        to_fix=Count("pk", filter=DOCUMENT_TO_FIX),
    )
    waiting = counts["tickets"] + counts["to_fix"]
    # « Enseignes et fournisseurs » counts every supplier, grey - counted,
    # not listed: the list reads every document's text, and the tabs are on
    # every page of Achats. Both of its tables are every supplier but the AI
    # pseudo-supplier. Beside it, amber, « N à voir »: the suppliers with a
    # change to see, which the tab lists at its top (#a-voir). One number
    # meant both - 29 grey, then an amber 1 - with no word nor title, and the
    # owner could not tell what the « 1 » was (19/09). The fragment stays:
    # the list sits below the import card, 850 px down; the sticky topbar's
    # height is left above it by the stylesheet (--topbar-room), since the
    # list's heading first landed under the bar.
    suppliers = Supplier.objects.exclude(parser_key=LLM_PARSER_KEY)
    to_see = _changes_to_see().filter(supplier__in=suppliers).values("supplier_id").distinct().count()
    suppliers_url = reverse("invoices:supplier_list")
    context = {
        "tab": tab,
        "tabs": [
            {"key": "documents", "label": "Documents", "url": reverse("invoices:invoice_list"),
             "count": counts["total"], "attention": False},
            {"key": "a-verifier", "label": "À vérifier", "url": reverse("invoices:receipt_queue"),
             "count": waiting, "attention": bool(waiting)},
            {"key": "sources", "label": "Sources", "url": reverse("invoices:invoice_type_list"),
             "count": InvoiceType.objects.count(), "attention": False},
            {"key": "fournisseurs", "label": "Enseignes et fournisseurs",
             "url": suppliers_url + ("#a-voir" if to_see else ""),
             "count": suppliers.count(), "attention": False, "to_see": to_see},
        ],
        **_import_card(request, **card),
    }
    for entry in context["tabs"]:
        entry["active"] = entry["key"] == tab
    if tab == "documents":
        context.update(_documents(request, card.get("batch")))
    elif tab == "a-verifier":
        context.update(_to_check())
    elif tab == "fournisseurs":
        context.update(_suppliers())
    else:
        context.update(_sources())
    return render(request, "invoices/purchases.html", context, status=status)


def _missed(job: ScrapeJob) -> frozenset:
    """What a gather did not get: its sources in error, "*" for a run that
    failed or was cancelled as a whole. Empty for a clean run."""
    missed = {code for code, entry in (job.progress or {}).items() if entry.get("error")}
    if job.status in (ScrapeJob.Status.FAILED, ScrapeJob.Status.CANCELLED):
        missed.add("*")
    return frozenset(missed)


def _missed_again(job: ScrapeJob) -> bool:
    """The gather before it asked for the same period and missed the same."""
    previous = (
        ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER, started_at__lte=job.started_at)
        .exclude(pk=job.pk)
        .order_by("-started_at", "-pk")
        .first()
    )
    return previous is not None and previous.range_start == job.range_start and _missed(previous) == _missed(job)


def _import_card(request, import_tab=None, batch=None, receipt_form=None, pdf_form=None) -> dict:
    from .receipts import invoice_supplier_choices

    metro = Supplier.objects.filter(OWN_MODULE).first()
    # The mailbox's types and the customer portals': both are gathered.
    email_types = list(InvoiceType.objects.filter(is_active=True).select_related("supplier"))
    gather_sources = []
    if metro:
        from .scrapers.metro import metro_pause

        # Left alone after its firewall refused, or signed in to lately: the
        # box is out of reach and the reason said (_import_card.html).
        gather_sources.append({"code": "METRO", "label": metro.name, "paused": metro_pause()})
    gather_sources += [{"code": f"type-{it.id}", "label": it.name} for it in email_types]
    # From the newest invoice these sources have already brought in: a
    # gather is for what arrived since. The earliest of each source's
    # latest used to be taken instead - one supplier billing twice a
    # year sent every gather ten months back, through 3,800 emails.
    gathered = {it.supplier_id for it in email_types}
    if metro:
        gathered.add(metro.pk)
    ScrapeJob.reap_stale()
    latest_job = ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).order_by("-started_at", "-pk").first()
    gather_start, gather_end = default_gather_start(gathered), timezone.localdate()
    if latest_job is not None and latest_job.range_start and (
        latest_job.is_active or (_missed(latest_job) and not _missed_again(latest_job))
    ):
        # A run not over, or that did not get everything: its period is
        # offered again. The default - since the newest invoice brought in -
        # skipped what it missed, and the dates typed were gone after the
        # redirect: a retry of 01/01/2026 searched from June. Not when the
        # same sources failed the same period twice: a portal asking for a
        # code every time held every gather on 01/01 for good.
        gather_start = latest_job.range_start
        asked_until = latest_job.range_end
        if asked_until and asked_until < timezone.localdate(latest_job.started_at):
            gather_end = asked_until  # a past period, asked on purpose

    recent_batches = list(ReceiptBatch.objects.all()[:5])
    shown = batch
    if shown is None and recent_batches:
        # An import still running, stopped halfway or with tickets to file
        # stays in sight; a finished one is in "Derniers imports".
        latest = recent_batches[0]
        if latest.is_active or latest.can_resume or latest.awaiting_shop_count:
            shown = latest

    import_tab = OLD_IMPORT_TABS.get(import_tab, import_tab)
    if import_tab not in IMPORT_TABS:
        import_tab = request.GET.get("ajouter", "")
        import_tab = OLD_IMPORT_TABS.get(import_tab, import_tab)
    if import_tab not in IMPORT_TABS:
        if shown is not None:
            import_tab = "documents"
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
        "default_start_date": gather_start,
        "default_end_date": gather_end,
        "latest_job": latest_job,
        "recent_batches": recent_batches,
        "batch": shown,
    }
    if shown is not None:
        card.update(batch_status_context(shown))
    return card


def batch_status_context(batch) -> dict:
    """What the live part of an import draws: the import, its files with the
    documents they became, the shops a file no shop was recognised on can be
    filed under, and where checking its tickets starts."""
    from .receipts import shop_choices

    first, left = first_ticket_to_check(batch_invoice_ids(batch))
    return {
        "batch": batch,
        "batch_rows": batch_rows(batch),
        "shop_groups": shop_choices() if batch.awaiting_shop_count else [],
        "batch_first_to_check": first,
        "batch_to_check": left,
    }


def batch_rows(batch) -> list[dict]:
    """The import's files, each with the document it became - as that
    document is now.

    What the import wrote down (shop, date, total, state) was true the second
    it read the file, and the page went on showing it: a ticket corrected
    afterwards still read its first total and "À vérifier" on the import it
    came from. The log keeps its own record; this is what is shown.
    """
    invoices = Invoice.objects.filter(pk__in=batch_invoice_ids(batch)).select_related("supplier").in_bulk()
    return [dict(entry, invoice=invoices.get(entry.get("invoice_id"))) for entry in batch.results]


#: How many documents a list shows before it asks to be asked. Every row is
#: about 1,4 Ko of HTML and a slice of a second of template, and the list
#: grows with every import: at 823 documents the page was 1,2 Mo and took
#: half a second to render, so opening a row moved a table 47 000 pixels
#: tall. "Tout afficher" renders the rest.
PAGE_SIZE = 250

def documents_matching(invoices, query: str):
    """The documents a typed search means: a supplier, a number, a date or an
    amount. The database answers, because the page holds only its first rows
    - and a box that searches what is rendered cannot find a document from
    last year.

    A date is matched as it is written (12/07/2026, 07/2026, 2026), and an
    amount against what the document charges, the way the list shows it.
    """
    query = query.strip()
    if not query:
        return invoices
    matches = Q(supplier__name__icontains=query) | Q(invoice_number__icontains=query)
    digits = [part for part in re.split(r"[^0-9]+", query) if part]
    written = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", query)
    if written:
        day, month, year = (int(part) for part in written.groups())
        matches |= Q(invoice_date__day=day, invoice_date__month=month, invoice_date__year=year)
    elif re.fullmatch(r"(\d{1,2})[/.-](\d{4})", query):
        month, year = (int(part) for part in digits)
        matches |= Q(invoice_date__month=month, invoice_date__year=year)
    elif re.fullmatch(r"(19|20)\d{2}", query):
        matches |= Q(invoice_date__year=int(query))
    amount = re.fullmatch(r"-?\d{1,6}(?:[.,]\d{1,2})?", query)
    if amount:
        matches |= Q(printed_total_ttc=Decimal(query.replace(",", ".")))
    return invoices.filter(matches)


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
    # One supplier's documents exactly, from its page: a search for "Free"
    # found Free Mobile's too.
    chosen = request.GET.get("fournisseur", "")
    supplier_filter = Supplier.objects.filter(pk=chosen).first() if is_id(chosen) else None
    if supplier_filter is not None:
        invoices = invoices.filter(supplier=supplier_filter)
    query = request.GET.get("q", "")
    if query:
        invoices = documents_matching(invoices, query)
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
        parameters = {key: value for key, value in (("filtre", chip["key"]), ("q", query)) if value}
        chip["url"] = reverse("invoices:invoice_list") + (f"?{urlencode(parameters)}" if parameters else "")

    found = invoices.count() if query else None
    everything = request.GET.get("tout") == "1" or batch is not None or active == "sans-date"
    shown = invoices if everything else invoices[:PAGE_SIZE]
    rows = list(shown)
    listed = found if query else counts["total" if not active else active.replace("-", "_")]
    hidden = 0 if everything else max(listed - len(rows), 0)
    posted = request.GET.get("surligner", "")
    highlight = int(posted) if is_id(posted) else None
    if highlight is not None:
        # The document just imported is shown whatever its date: dated last
        # year, it sits past the rows this page renders, and "importée" would
        # point at nothing.
        if not any(invoice.pk == highlight for invoice in rows):
            rows = [
                *Invoice.objects.filter(pk=highlight).select_related("supplier").prefetch_related("lines"),
                *rows,
            ]
        # Stable: the highlighted document first, the rest in their order.
        rows.sort(key=lambda invoice: invoice.pk != highlight)
    return {
        "invoices": rows,
        "query": query,
        "found_count": found,
        "hidden_count": hidden,
        "show_all_url": request.get_full_path() + ("&" if request.GET else "?") + "tout=1",
        "chips": chips,
        "active_filter": active,
        "highlight": highlight,
        "lot": batch,
        "undated_count": counts["sans_date"],
        "supplier_filter": supplier_filter,
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
    """The sources of invoices (InvoiceType), each with its channel as the
    form words it (forms.CHANNELS)."""
    invoice_types = list(InvoiceType.objects.select_related("supplier", "email_source"))
    for invoice_type in invoice_types:
        invoice_type.channel = CHANNELS.get(invoice_type.source_kind, invoice_type.get_source_kind_display())
    return {"invoice_types": invoice_types}


def _changes_to_see():
    """What marks a supplier's row « À voir », and lights its tab: a change
    to see, neither seen nor undone."""
    from .models import SupplierChange

    return SupplierChange.objects.filter(needs_review=True, reviewed_at__isnull=True, undone_at__isnull=True)


def _suppliers() -> dict:
    """Every supplier with what files a document under it on its own - its
    header and the figures it learned - shown, since what cannot be seen
    cannot be put right, and the sources fetching for it. For a shop with a
    header, how many of its documents print it: the others were filed there
    by something else they print.

    Above them, every change to see, oldest first, with why it asks and its
    « Vu »: what lights the tab's « à voir », said where it lights up rather
    than as a pill on one row among thirty."""
    from .parsers import LLM_PARSER_KEY, is_ticket_shop
    from .receipts import has_own_reader, names_shop, prints_header
    from .supplier_changes import why_to_see

    changes_to_see = list(
        _changes_to_see()
        .exclude(supplier__parser_key=LLM_PARSER_KEY)
        .select_related("supplier")
        .order_by("created_at", "pk")
    )
    for change in changes_to_see:
        change.why = why_to_see(change)

    texts: dict[int, list[str]] = {}
    for supplier_id, ocr_text, source_text in Invoice.objects.values_list("supplier_id", "ocr_text", "source_text"):
        if ocr_text or source_text:
            texts.setdefault(supplier_id, []).append(ocr_text or source_text)
    from .parsers import ticket_parser_for

    counts = dict(Invoice.objects.values_list("supplier_id").annotate(n=Count("id")).values_list("supplier_id", "n"))
    latest = dict(
        Invoice.objects.values_list("supplier_id").annotate(last=Max("invoice_date")).values_list("supplier_id", "last")
    )
    to_see = dict(
        _changes_to_see()
        .values_list("supplier_id")
        .annotate(n=Count("id"))
        .values_list("supplier_id", "n")
    )
    sources: dict[int, list] = {}
    for invoice_type in InvoiceType.objects.order_by("name"):
        sources.setdefault(invoice_type.supplier_id, []).append(invoice_type)
    suppliers = list(Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).order_by("name"))
    for supplier in suppliers:
        # Each row leads to the supplier's own page (supplier_views).
        supplier.url = reverse("invoices:supplier_detail", args=[supplier.pk])
        supplier.last_date = latest.get(supplier.pk)
        supplier.to_see = to_see.get(supplier.pk, 0)
        supplier.is_till = ticket_parser_for(supplier.code) is not None
        supplier.sources = sources.get(supplier.pk, [])
    shops = [supplier for supplier in suppliers if is_ticket_shop(supplier)]
    for shop in shops:
        # Attributes, since a template calls nothing with arguments.
        shop.names_shop = names_shop(shop)
        shop.document_count = counts.get(shop.pk, 0)
        shop.with_header = shop.headerless = 0
        if shop.ticket_header:
            shop.with_header = sum(1 for text in texts.get(shop.pk, ()) if prints_header(text, shop.ticket_header))
            shop.headerless = len(texts.get(shop.pk, ())) - shop.with_header
    own_readers = [supplier for supplier in suppliers if has_own_reader(supplier)]
    own_module = set(Supplier.objects.filter(OWN_MODULE).values_list("pk", flat=True))
    for supplier in own_readers:
        supplier.names_shop = names_shop(supplier)
        supplier.own_module = supplier.pk in own_module
    return {
        "changes_to_see": changes_to_see,
        "ticket_shops": shops,
        "own_readers": own_readers,
    }
