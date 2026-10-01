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
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

import common
from accounts.tenancy import integrations_allowed
from common import RANGE_END, RANGE_START, DateRange, date_range, is_id, search_key

from . import integrations
from .forms import CHANNELS, InvoiceUploadForm, ReceiptBatchUploadForm
from .models import Invoice, InvoiceType, ReceiptBatch, ScrapeJob, Supplier
from .tasks import default_gather_start, slips_code, slips_label

#: "Ajoutés récemment", "Vérifiés récemment": what an import or a checking
#: session has just done, to see it landed.
RECENT = timedelta(hours=48)
# One import for every document - a photo, a scan, a supplier's PDF: what
# each file is decides how it is read (receipts.import_document). "tickets"
# and "pdf" were two, and are kept as names of the same one.
IMPORT_TABS = ("documents", "recuperer")
OLD_IMPORT_TABS = {"tickets": "documents", "pdf": "documents"}

#: Read from an EN 16931 document's own data (Invoice.einvoice_format): its
#: figures are the invoice's, not a reading. It carries checks - about the
#: SUPPLIER's arithmetic - and is an invoice all the same, so every list that
#: tells a ticket from an invoice by those checks has to ask this too.
IS_EINVOICE = ~Q(einvoice_format="")
IS_TICKET = (~Q(parse_checks=[]) | ~Q(ocr_text="")) & ~IS_EINVOICE
IS_INVOICE = Q(parse_checks=[], ocr_text="") | IS_EINVOICE
#: A supplier of charges: a rent, a subscription (Supplier.expenses_only).
IS_CHARGE = Q(supplier__expenses_only=True)
#: The review queue. `receipts.pending_receipts` is this Q and nothing else:
#: written twice, the tab counted the charges its own list left out, and
#: said "À vérifier 102" over an empty page. An electronic invoice is out of
#: it for the same reason a charge is: there is nothing to re-type from a
#: photograph it never had, and its figures are the document's own.
TICKET_TO_CHECK = Q(reviewed_at__isnull=True) & ~Q(parse_checks=[]) & ~IS_CHARGE & ~IS_EINVOICE
#: A document outside that queue that cannot be used as it is: undated (out
#: of every valuation and of the bank match), failed to import, a charge
#: whose own total could not be read (importing.charge_needs_a_look), an
#: electronic invoice whose own totals do not hold or that states no date
#: (Invoice.error_message - the supplier's arithmetic, reported and never
#: repaired, which nobody can re-type either), or one an invoice type fetched
#: whose supplier is in doubt (Invoice.supplier_doubt: a ticket among them
#: waits in the queue while it is unchecked) - it is in no queue, so this is
#: where it is seen.
DOCUMENT_TO_FIX = (
    (Q(parse_checks=[]) & (Q(invoice_date__isnull=True) | Q(status=Invoice.Status.ERROR)))
    | (IS_CHARGE & Q(status=Invoice.Status.NEEDS_REVIEW))
    # A supplier's PDF that says what is wrong with it: its lines do not add
    # up to the total it prints (invoices/parsers/metro.py). It cannot go in
    # the ticket queue - `parse_checks` is what tells a ticket from an
    # invoice, and filling it would queue every Metro invoice behind the
    # photographs - so without this it was counted in no queue at all, which
    # is the silence the warning exists to break. Dated and finished, it is
    # done: a scan typed in by hand keeps the message the import left.
    | (~IS_CHARGE & ~IS_EINVOICE & Q(parse_checks=[], status=Invoice.Status.NEEDS_REVIEW) & ~Q(error_message=""))
    | (IS_EINVOICE & (~Q(error_message="") | Q(invoice_date__isnull=True) | Q(status=Invoice.Status.ERROR)))
    | (~Q(supplier_doubt="") & ~TICKET_TO_CHECK)
)
#: The supplier « Récupérer les nouvelles factures » fetches with a module of
#: its own rather than through a source (tasks.gather_invoices_task): Metro.
#: UBA is seeded `is_scrapable` too, but a mailbox source fetches it.
OWN_MODULE = Q(code="METRO", is_scrapable=True)


def own_module_suppliers():
    """The suppliers fetched by a module of their own (OWN_MODULE) - none in
    a tenant that may not use the server's accounts (integrations.py): that
    module signs in to the owner's Metro account, whatever the tenant's
    METRO row says (an import can tick `is_scrapable` again)."""
    if not integrations_allowed():
        return Supplier.objects.none()
    return Supplier.objects.filter(OWN_MODULE)


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
            {
                "key": "documents",
                "label": "Documents",
                "url": reverse("invoices:invoice_list"),
                "count": counts["total"],
                "attention": False,
            },
            {
                "key": "a-verifier",
                "label": "À vérifier",
                "url": reverse("invoices:receipt_queue"),
                "count": waiting,
                "attention": bool(waiting),
            },
            {
                "key": "sources",
                "label": "Sources",
                "url": reverse("invoices:invoice_type_list"),
                "count": InvoiceType.objects.count(),
                "attention": False,
            },
            {
                "key": "fournisseurs",
                "label": "Enseignes et fournisseurs",
                "url": suppliers_url + ("#a-voir" if to_see else ""),
                "count": suppliers.count(),
                "attention": False,
                "to_see": to_see,
            },
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


#: How many of the latest gathers are looked through for the latest one
#: searching invoices (_invoice_gather).
RECENT_GATHERS = 20


def _invoice_gather(jobs):
    """The first of `jobs` (newest first) that searched invoices - not a
    gather of returnables slips only (ScrapeJob.slips_only, the Consignes
    page's): its period is the slips' own start (returnables.mail.
    fetch_start), and a failed one offered on Achats pushed the invoices'
    default start back to it - 90 days on a first run. At most the latest
    RECENT_GATHERS, looked through in Python (the progress is JSON)."""
    return next((job for job in jobs[:RECENT_GATHERS] if not job.slips_only), None)


def _missed_again(job: ScrapeJob) -> bool:
    """The gather before it asked for the same period and missed the same -
    the gathers searching invoices only (_invoice_gather)."""
    previous = _invoice_gather(
        ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER, started_at__lte=job.started_at)
        .exclude(pk=job.pk)
        .defer("log", "test_matches")
        .order_by("-started_at", "-pk")
    )
    return previous is not None and previous.range_start == job.range_start and _missed(previous) == _missed(job)


def _import_card(request, import_tab=None, batch=None, receipt_form=None, pdf_form=None) -> dict:
    from .receipts import invoice_supplier_choices

    # Every source a gather searches is one of the server's own accounts:
    # in a tenant that may not use them the panel says « à configurer »
    # (_import_card.html), and nothing about them is read.
    allowed = integrations_allowed()
    metro = own_module_suppliers().first()
    # The mailbox's types and the customer portals': both are gathered.
    email_types = list(InvoiceType.objects.filter(is_active=True).select_related("supplier")) if allowed else []
    gather_sources = []
    if metro:
        from .scrapers.metro import metro_pause

        # Left alone after its firewall refused, or signed in to lately: the
        # box is out of reach and the reason said (_import_card.html).
        gather_sources.append({"code": "METRO", "label": metro.name, "paused": metro_pause()})
    gather_sources += [{"code": f"type-{it.id}", "label": it.name} for it in email_types]
    if allowed:
        # The drivers' returnables slips, each format fetched by mail: they
        # go to Consignes, never among the invoices (tasks._gather_slips).
        from returnables.models import SlipFormat

        gather_sources += [
            {"code": slips_code(fmt), "label": slips_label(fmt)}
            for fmt in SlipFormat.objects.filter(is_active=True).exclude(sender_pattern="").order_by("name", "pk")
        ]
    # From the newest invoice these sources have already brought in: a
    # gather is for what arrived since. The earliest of each source's
    # latest used to be taken instead - one supplier billing twice a
    # year sent every gather ten months back, through 3,800 emails.
    gathered = {it.supplier_id for it in email_types}
    if metro:
        gathered.add(metro.pk)
    ScrapeJob.reap_stale()
    gathers = ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).order_by("-started_at", "-pk")
    latest_job = gathers.first()
    # The period offered again is an invoice gather's only: a gather of
    # returnables slips alone (the Consignes page's) starts from the slips'
    # own start, and never holds Achats' period.
    period_job = latest_job
    if latest_job is not None and latest_job.slips_only:
        period_job = _invoice_gather(gathers.defer("log", "test_matches"))
    gather_start, gather_end = default_gather_start(gathered), timezone.localdate()
    if (
        period_job is not None
        and period_job.range_start
        and (period_job.is_active or (_missed(period_job) and not _missed_again(period_job)))
    ):
        # A run not over, or that did not get everything: its period is
        # offered again. The default - since the newest invoice brought in -
        # skipped what it missed, and the dates typed were gone after the
        # redirect: a retry of 01/01/2026 searched from June. Not when the
        # same sources failed the same period twice: a portal asking for a
        # code every time held every gather on 01/01 for good.
        gather_start = period_job.range_start
        asked_until = period_job.range_end
        if asked_until and asked_until < timezone.localdate(period_job.started_at):
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
        "gather_refused": None if allowed else integrations.GATHER,
        "ai_refused": None if allowed else integrations.AI_READING,
        # « Prendre une photo » stops what the form would post short of
        # Cloudflare's limit (photos.js, data-max-bytes). Read at the call,
        # as common's caps are, so a test can patch it.
        "camera_max_bytes": common.ONLINE_SEND_MAX_BYTES,
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

    A document deleted since is `gone`: the record still names it, and drawn
    from the record the row read « À vérifier » over a « Vérifier » that led
    to a 404.
    """
    invoices = Invoice.objects.filter(pk__in=batch_invoice_ids(batch)).select_related("supplier").in_bulk()
    rows = []
    for entry in batch.results:
        invoice = invoices.get(entry.get("invoice_id"))
        gone = entry.get("status") == "ok" and bool(entry.get("invoice_id")) and invoice is None
        rows.append(dict(entry, invoice=invoice, gone=gone))
    return rows


def batch_deleted(batch, pk) -> bool:
    """Whether this import made document `pk` and it has been deleted since -
    what an old « Vérifier » link into the import is told."""
    return pk in batch_invoice_ids(batch) and not Invoice.objects.filter(pk=pk).exists()


#: How many documents a list shows before it asks to be asked. Every row is
#: about 1,4 KB of HTML and a slice of a second of template, and the list
#: grows with every import: at 823 documents the page was 1,2 MB and took
#: half a second to render, so opening a row moved a table 47 000 pixels
#: tall. "Tout afficher" renders the rest.
PAGE_SIZE = 250

#: Past this many words a query is something pasted, not a search. Every
#: word is a condition ANDed onto the query, and a pasted paragraph builds
#: one too deep for SQLite to compile. Dropping the tail can only WIDEN the
#: answer, never hide a document the words named - which is the safe way for
#: a cap to be wrong.
MAX_TERMS = 12

#: A date is a whole word or nothing. A document number carries digits,
#: dashes and slashes of its own ("047-031286"), and read as a date it would
#: answer an empty page for the document the reader is holding.
_A_DAY = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_A_MONTH = re.compile(r"(\d{1,2})[/.-](\d{4})")
#: A year the bar could have traded in. « 1312 » is not a year here, it is
#: four digits somebody typed.
_A_YEAR = re.compile(r"(?:19|20)\d{2}")
_AN_AMOUNT = re.compile(r"-?\d{1,6}(?:[.,]\d{1,2})?")
#: An amount as the pages print it, its thousands grouped by a space of any
#: kind (« 1 234.56 », common.group_thousands): copied off the page it is one
#: amount, never « 1 » and « 234.56 ». With its decimals only, so a year and
#: a number typed side by side (« 2025 123 ») stay two words.
_A_GROUPED_AMOUNT = re.compile(r"(?<![\d.,/-])-?\d{1,3}(?:\s\d{3})+[.,]\d{1,2}(?![\d.,])")


def _a_date(term: str) -> dict | None:
    """The date lookups `term` means, or None when it is not exactly a date.

    A day that is no day (« 32/07/2026 ») and a month that is no month
    (« 13/2025 ») are not dates either: they fall through to being matched
    against the name and the number, find nothing, and the search answers
    empty rather than raising on a query string somebody typed.
    """
    written = _A_DAY.fullmatch(term)
    if written:
        day, month, year = (int(part) for part in written.groups())
        try:
            date(year, month, day)
        except ValueError:
            return None
        return {"invoice_date__day": day, "invoice_date__month": month, "invoice_date__year": year}
    month_of = _A_MONTH.fullmatch(term)
    if month_of:
        month, year = (int(part) for part in month_of.groups())
        if 1 <= month <= 12:
            return {"invoice_date__month": month, "invoice_date__year": year}
        return None
    if _A_YEAR.fullmatch(term):
        return {"invoice_date__year": int(term)}
    return None


def _suppliers_named(term: str) -> list[int]:
    """The suppliers whose name holds `term`, ignoring case AND accents.

    In Python, over a bar's few dozen suppliers, because SQLite's `icontains` folds
    case but not accents: « epicerie » typed in a hurry has to find
    « Épicerie du coin », and nobody reaches for the compose key standing at
    a bar. The DOCUMENTS are never compared this way - there are hundreds of
    them and the page holds only its first rows, so the database has to
    answer that part.
    """
    wanted = search_key(term)
    return [pk for pk, name in Supplier.objects.values_list("pk", "name") if wanted in search_key(name)]


def documents_matching(invoices, query: str):
    """The documents a typed search means: a supplier, a number, a date, an
    amount - **and any of them together**.

    The words narrow each other. « Grossiste 2025 » is that supplier's
    documents of 2025, not every document of 2025 plus every document of that
    supplier; « Grossiste 09/2025 » is its September. Read as one string, as
    this was, neither found anything at all: no supplier is called « Grossiste
    2025 », no number holds it, and the whole of it is no date.

    Each word is matched against the name, the number, the date AND the
    amount, and the words are ANDed. A word is never *only* a date: a
    document numbered « FA-2031-118 » is still found by « 2031 », which is
    the number printed on the paper in the reader's hand.

    The database answers, because the page holds only its first rows - a box
    that searches what is rendered cannot find a document from last year.
    """
    query = _A_GROUPED_AMOUNT.sub(lambda amount: "".join(amount.group().split()), query)
    terms = query.split()[:MAX_TERMS]
    for term in terms:
        matches = Q(invoice_number__icontains=term)
        named = _suppliers_named(term)
        if named:
            matches |= Q(supplier_id__in=named)
        day = _a_date(term)
        if day:
            matches |= Q(**day)
        if _AN_AMOUNT.fullmatch(term):
            matches |= Q(printed_total_ttc=Decimal(term.replace(",", ".")))
        invoices = invoices.filter(matches)
    return invoices


FILTERS = {
    "factures": ("Factures", IS_INVOICE),
    "tickets": ("Tickets", IS_TICKET),
    "recents": ("Ajoutés récemment", None),
    "verifies": ("Vérifiés récemment", None),
    "non-rapprochees": ("Non rapprochées", None),
    "sans-date": ("Sans date", Q(invoice_date__isnull=True)),
}


#: `unreconciled_q()` looks the first statement up itself; a page that
#: already knows it passes it in. None is a real answer there - « no
#: statement at all » - so the two cases need telling apart.
_LOOK_UP = object()


def unreconciled_q(start=_LOOK_UP) -> Q:
    """Documents no bank line pays, among those the statements could pay.

    « No payment at all » is the bank's own rule, not a second one:
    `reconcile.unpaid_invoices` is deliberately not « no payment from THIS
    line », because an invoice settled by two lines is something only a
    person says. What is added here is the window - before the first
    statement, unreconciled is not a fact about a document, it is a fact
    about the statements, and nearly every document with no payment is only
    that on a real database. Counted whole the chip reads hundreds over the
    handful somebody can act on, which teaches the reader to ignore it.

    An undated document is kept: it is inside no window, and the matching
    only ever suggests those - left out, exactly the documents that need a
    person would be the ones hidden.
    """
    if start is _LOOK_UP:
        from bank.reconcile import statements_start  # here: bank reads this module

        start = statements_start()
    if start is None:
        # Nothing imported, so nothing is reconcilable. Never an empty
        # `pk__in`: that is an EmptyResultSet, which a Count(filter=...)
        # cannot carry.
        return Q(pk__isnull=True)
    return Q(payments__isnull=True) & (Q(invoice_date__gte=start) | Q(invoice_date__isnull=True))


def bank_state(invoice, start) -> dict | None:
    """What a row says about its settlement - beside what `review_state`
    says about its reading, never instead of it.

    None where the statements have nothing to say: no statement imported at
    all, or a document older than the first one. « Non rapprochée » written
    on every document of 2024 is noise, and noise on hundreds of rows is what
    hides the handful that are really waiting.
    """
    payments = list(invoice.payments.all())
    if payments:
        paid_on = min(payment.transaction.paid_on for payment in payments)
        return {"css": "COMPLETE", "label": f"Réglée le {paid_on:%d/%m/%Y}"}
    if start is None or (invoice.invoice_date is not None and invoice.invoice_date < start):
        return None
    return {"css": "NEEDS_REVIEW", "label": "Non rapprochée"}


def _list_url(request, *dropped: str) -> str:
    """This same list of documents without those query parameters: where an
    « Effacer » leads.

    One button clearing the lot silently undid the other - the window typed
    in the date form went with the search, and the search with the dates - so
    each drops its own and its label says which. `surligner` goes with every
    one of them: that pill is about the document an import just added, and
    narrowing the list is not about it.
    """
    kept = {key: value for key, value in request.GET.items() if value and key != "surligner" and key not in dropped}
    return reverse("invoices:invoice_list") + (f"?{urlencode(kept)}" if kept else "")


def _documents(request, batch) -> dict:
    from bank.reconcile import statements_start  # here: bank reads this module

    # Looked up once and carried: the chip's condition and every row's
    # settlement ask the same question, and asked twice it is two queries
    # that can disagree between them within one page.
    reconciled_from = statements_start()
    since = timezone.now() - RECENT
    conditions = {
        **{key: condition for key, (_label, condition) in FILTERS.items() if condition is not None},
        "recents": Q(imported_at__gte=since),
        "verifies": Q(reviewed_at__gte=since),
        "non-rapprochees": unreconciled_q(reconciled_from),
    }
    active = request.GET.get("filtre", "")
    if request.GET.get("sans_date"):
        active = "sans-date"
    if batch is not None or active not in conditions:
        active = ""
    # « Du … au … » (common.date_range), both ends included. Two lists it
    # does not narrow, and it is dropped outright for them rather than
    # skipped at each use, so the form, the chips, the note and the rows
    # cannot disagree about whether there is a window at all:
    #
    # * an import's own list is the documents that import brought in - an
    #   explicit list, not a search, which is why it already ignores the
    #   filter chips too;
    # * « Sans date » is where a document with no date is looked at, and a
    #   document with no date is in no window: under one, that chip would
    #   open an empty page for ever and read as a broken page rather than as
    #   the one list two dates cannot narrow.
    window = date_range(request)
    if batch is not None or active == "sans-date":
        window = DateRange()

    # « Non rapprochées » is counted on its own, never folded into the
    # aggregate below: it asks about `payments`, a multi-valued relation, and
    # one Count(filter=...) over it puts a LEFT JOIN under ALL of them - an
    # invoice settled by two lines is then two rows, and every other chip on
    # the page counts it twice. One extra cheap query instead of six silently
    # inflated figures.
    in_window = window.limit(Invoice.objects.all(), "invoice_date")
    counted = {key: q for key, q in conditions.items() if key != "non-rapprochees"}
    counts = in_window.aggregate(
        total=Count("pk"), **{key.replace("-", "_"): Count("pk", filter=q) for key, q in counted.items()}
    )
    counts["non_rapprochees"] = in_window.filter(conditions["non-rapprochees"]).count()
    # Counted over every document whatever the window, since its chip drops
    # the window: narrowed by one it is always 0, so the chip and the warning
    # below would vanish exactly when there are documents no valuation and no
    # bank match can use.
    counts["sans_date"] = Invoice.objects.filter(conditions["sans-date"]).count()

    invoices = window.limit(
        # `payments__transaction` is what each row says about its settlement
        # (bank_state): one prefetched query for the page rather than one
        # per row - see « N+1s hide in per-object properties ».
        Invoice.objects.select_related("supplier").prefetch_related("lines", "payments__transaction"),
        "invoice_date",
    )
    # One supplier's documents exactly, from its page: a search for "Free"
    # found Free Mobile's too.
    chosen = request.GET.get("fournisseur", "")
    supplier_filter = Supplier.objects.filter(pk=chosen).first() if is_id(chosen) else None
    if supplier_filter is not None:
        invoices = invoices.filter(supplier=supplier_filter)
    query = request.GET.get("q", "")
    if query:
        invoices = documents_matching(invoices, query)
    if batch is not None:
        invoices = invoices.filter(pk__in=batch_invoice_ids(batch))
    elif active:
        invoices = invoices.filter(conditions[active])
        if active == "verifies":
            invoices = invoices.order_by("-reviewed_at")

    chips = [{"key": "", "label": "Tous", "count": counts["total"]}]
    for key, (label, _condition) in FILTERS.items():
        count = counts[key.replace("-", "_")]
        # The kinds always; the others when they hold something.
        if key in ("factures", "tickets") or count or key == active:
            chips.append({"key": key, "label": label, "count": count})
    for chip in chips:
        chip["active"] = batch is None and chip["key"] == active
        parameters = {key: value for key, value in (("filtre", chip["key"]), ("q", query)) if value}
        # Every chip keeps the window, or it silently vanishes on the next
        # click - except « Sans date », above.
        if chip["key"] != "sans-date":
            parameters.update(window.parameters)
        chip["url"] = reverse("invoices:invoice_list") + (f"?{urlencode(parameters)}" if parameters else "")

    found = invoices.count() if query or supplier_filter is not None else None
    everything = request.GET.get("tout") == "1" or batch is not None or active == "sans-date"
    shown = invoices if everything else invoices[:PAGE_SIZE]
    rows = list(shown)
    # What this list holds. The chip counts follow the window, but know
    # nothing of a search nor of one supplier's page, and « 875 de plus dans
    # cette liste » over five documents is the same lie as a chip over an
    # empty page.
    listed = found if found is not None else counts["total" if not active else active.replace("-", "_")]
    hidden = 0 if everything else max(listed - len(rows), 0)
    posted = request.GET.get("surligner", "")
    highlight = int(posted) if is_id(posted) else None
    if highlight is not None:
        # The document just imported is shown whatever its date: dated last
        # year, it sits past the rows this page renders, and "importée" would
        # point at nothing. Never under a window, though: the reader asked
        # about two dates, and a document from outside them slipped into the
        # answer is exactly the silently wrong figure this page guards against.
        if not window and not any(invoice.pk == highlight for invoice in rows):
            rows = [
                *Invoice.objects.filter(pk=highlight)
                .select_related("supplier")
                .prefetch_related("lines", "payments__transaction"),
                *rows,
            ]
        # Stable: the highlighted document first, the rest in their order.
        rows.sort(key=lambda invoice: invoice.pk != highlight)
    # Said on every row, on every list: a person looking at « Tous » sees
    # which documents the bank has settled without having to filter for it.
    # Annotated here, after the highlighted document has been folded in, so
    # no row can reach the template without it.
    for invoice in rows:
        invoice.bank_state = bank_state(invoice, reconciled_from)
    return {
        "invoices": rows,
        "query": query,
        "found_count": found if query else None,
        "hidden_count": hidden,
        "listed_count": listed,
        "show_all_url": request.get_full_path() + ("&" if request.GET else "?") + "tout=1",
        "chips": chips,
        "active_filter": active,
        # The first day the statements cover, for the note « Non rapprochées »
        # owes its reader: a list that silently drops every document older
        # than the statements reads as a bug rather than as an answer.
        "reconciled_from": reconciled_from,
        "highlight": highlight,
        "lot": batch,
        "undated_count": counts["sans_date"],
        "supplier_filter": supplier_filter,
        "date_window": window,
        "show_everything": everything,
        "clear_window_url": _list_url(request, RANGE_START, RANGE_END),
        "clear_search_url": _list_url(request, "q"),
        "clear_supplier_url": _list_url(request, "fournisseur"),
    }


def _to_check() -> dict:
    from .receipts import pending_receipts

    receipts = list(
        pending_receipts().select_related("supplier").prefetch_related("lines").order_by("invoice_date", "id")
    )
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
    return {
        "invoice_types": invoice_types,
        # Both channels are the server's own accounts (integrations.py).
        "sources_refused": None if integrations_allowed() else integrations.SOURCES,
    }


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
    to_see = dict(_changes_to_see().values_list("supplier_id").annotate(n=Count("id")).values_list("supplier_id", "n"))
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
    own_module = set(own_module_suppliers().values_list("pk", flat=True))
    for supplier in own_readers:
        supplier.names_shop = names_shop(supplier)
        supplier.own_module = supplier.pk in own_module
    return {
        "changes_to_see": changes_to_see,
        "ticket_shops": shops,
        "own_readers": own_readers,
    }
