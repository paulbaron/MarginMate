"""« Consignes » (`/consignes/`): the empties handed back to the delivery
driver, counted and photographed here, then compared with the slip he sends.

Pages (every one behind the login, like the rest of the app):

* `/consignes/` (`home`), phone-first: the latest pickup in one line, the
  « Nouvelle reprise » form (photos, one count per type, the kegs' bigger),
  the latest pickup in full, the pickups and the slips received (20 each,
  « tout afficher »), « Ajouter des bons » (upload, « Récupérer les bons »),
  and the settings' links. The strip and the lists reload themselves when a
  gather ends (`documents-changed`); the form is outside them, so a count
  half typed survives.
* `/consignes/reprises/<pk>/` (`pickup_detail`): the same form to edit it
  (photos can be ADDED, each removed on its own), its comparison with the
  day's slips, their invoice check, delete.
* `/consignes/bons/<pk>/` (`slip_detail`): what the reading found - checks,
  lines with the type and the pattern that classified each, remarks, the PDF -
  which slip replaces it, the invoice check, « Relire », « Classer comme ».
* `/consignes/formats/…` and `/consignes/types/`: the slip formats (patterns
  « Testés » on a stored slip, pasted text or a PDF before they are saved)
  and the returnable types (one form per type, no formset).

Rules:

* **POST, redirect, GET**, a French message saying what was done; a refused
  form is drawn back at 200 with its errors and writes nothing. A POST-only
  address answers a GET with a redirect and writes nothing. An id read off
  the POST by hand goes through `common.is_id`.
* **Every figure comes from the pure modules**: `comparison.Board` (loaded
  ONCE per page - a fixed number of queries however many rows), and
  `invoice_check.check_many` once with the board's index. Nothing is
  compared or classified here.
* **A stored pattern that fails is said, never a 500**: « motif invalide : … —
  corrigez-le », linked to where it is corrected.
* **The photos are prepared in memory before the transaction** (returnables/
  photos.py: re-encoded, no EXIF left), then saved inside it; if anything
  raises, every file already saved is deleted. A photo Pillow cannot read
  never refuses the pickup: the others are kept and a warning names it.
* **No `|safe`, no `mark_safe`**: every sentence built here or in the pure
  modules is plain text, escaped by the templates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

import regex
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.tenancy import integrations_allowed
from common import is_id
from invoices import integrations
from invoices.models import ScrapeJob
from returnables import comparison, invoice_check, patterns, reading, slips
from returnables.comparison import Board, pickup_summary, slip_label
from returnables.forms import (
    COUNT_PREFIX,
    NOTHING_LEFT,
    NOTHING_TO_SAVE,
    PickupForm,
    SlipFormatForm,
    TypeForm,
    check_pickup_date,
)
from returnables.mail import fetch_start
from returnables.models import (
    MAX_PHOTOS,
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
    delete_files,
    delete_with_files,
)
from returnables.patterns import PatternError
from returnables.photos import PhotoError, prepare_photo
from returnables.reading import SlipError, clean_text

logger = logging.getLogger(__name__)

#: What the forms with several buttons post to say which one was pressed.
ACTION = "action"
TEST = "tester"
SAVE = "enregistrer"
UNKNOWN_ACTION = "Action inconnue : rien n'a été modifié."

#: The photo inputs of a pickup's form (several, one name).
PHOTOS = "photos"
#: The upload's files, and its format (blank: recognised).
UPLOADS = "bons"
UPLOAD_FORMAT = "format"
#: The upload's messages are drawn in « Ajouter des bons » (#bons, where its
#: redirect lands, screens under the top of the page), not at the top;
#: « Nouveau format » is offered beside a document no format recognises.
UPLOAD_MESSAGES = "slips"
NEW_FORMAT = "new-format"
#: « Tester »'s sources: a stored slip, pasted text, a PDF.
TEST_SLIP = "tester_sur"
TEST_TEXT = "texte_essai"
TEST_PDF = "pdf_essai"
#: How many stored slips « Tester sur » offers.
TEST_SLIPS_OFFERED = 50
#: How many lines of the tested text « Tester » draws.
TRACE_LINES_SHOWN = 400

#: The home page's lists: the newest LISTED, « tout afficher » the rest.
LISTED = 20
SHOW_ALL = "tout"
#: The success redirect's flag: returnables.js forgets the draft on it.
SAVED_FLAG = "enregistree"

#: The gather's card stays on the page this long after it ended.
GATHER_SHOWN_FOR = timedelta(minutes=10)
#: The latest gathers searched for one that fetched slips.
GATHER_JOBS_SEARCHED = 20
#: The gather's codes for a format's slips (invoices/tasks.py).
SLIP_SOURCE_PREFIX = "bons-"

ALREADY_GATHERING = (
    "Une récupération est déjà en cours : elle récupère aussi les bons quand « Bons de consignes » est coché "
    "sur Achats."
)


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _messages_by_place(request) -> tuple[list, list]:
    """(the messages said at the top of the page, the upload's - said in
    « Ajouter des bons », where its redirect lands). Read once here, which
    also marks them said (margins.views._messages_by_place)."""
    top, uploads = [], []
    for message in messages.get_messages(request):
        (uploads if UPLOAD_MESSAGES in (message.extra_tags or "").split() else top).append(message)
    return top, uploads


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {plural if count > 1 else singular}"


def _day(value: date) -> str:
    return f"{value:%d/%m/%Y}"


def _home_url() -> str:
    return reverse("returnables:home")


def _back(request, default: str) -> str:
    """Where a small action returns: the home page when it says so (its
    card holds the same buttons as the pickup's page), else `default`."""
    return _home_url() if request.POST.get("retour") == _home_url() else default


# -- The gather (invoices/tasks.py fetches the slips; this page only asks) -------------------------------------------


def slips_refused() -> str:
    """What the page says where the server's mailbox may not be used (the
    gather itself refuses with integrations.GATHER)."""
    return integrations.SLIPS


def _slips_job():
    """(the gather to show, whether one is running): the latest GATHER that
    is running, or that fetched slips - shown while it runs and for ten
    minutes after it ended."""
    jobs = list(
        ScrapeJob.objects.filter(kind=ScrapeJob.Kind.GATHER).order_by("-started_at", "-pk")[:GATHER_JOBS_SEARCHED]
    )
    running = next((job for job in jobs if job.is_active), None)
    job = running or next(
        (job for job in jobs if any(str(code).startswith(SLIP_SOURCE_PREFIX) for code in (job.progress or {}))),
        None,
    )
    if job is not None and not job.is_active:
        ended = job.finished_at or job.started_at
        if ended is None or timezone.now() - ended > GATHER_SHOWN_FOR:
            job = None
    return job, running is not None


def _gather_context(today: date) -> dict:
    mail_formats = list(
        SlipFormat.objects.filter(is_active=True)
        .exclude(sender_pattern="")
        .select_related("supplier")
        .order_by("name", "pk")
    )
    allowed = integrations_allowed()
    job, running = _slips_job() if allowed else (None, False)
    # Each format's own start (returnables.mail: a few days before its newest
    # MAILED slip, else 90 days back); the gather searches from the earliest.
    starts = [fetch_start(fmt, None, today) for fmt in mail_formats] if allowed else []
    # « Déjà en cours » is said of another page's gather only: the one this
    # page just asked for (slips only, ScrapeJob.slips_only - or no source
    # written yet, its thread barely started) is shown by its card, and the
    # sentence read as if the tap had been ignored.
    another = running and bool(job.progress) and not job.slips_only
    return {
        "mail_formats": mail_formats,
        "gather_allowed": allowed,
        "gather_refused": "" if allowed else slips_refused(),
        "gather_job": job,
        "gather_running": running,
        "gather_running_sentence": ALREADY_GATHERING if another else "",
        "gather_start": min(starts) if starts else today,
        "gather_codes": [f"{SLIP_SOURCE_PREFIX}{fmt.pk}" for fmt in mail_formats],
    }


# -- Pieces the pages share -------------------------------------------------------------------------------------------


@dataclass
class ShownPhoto:
    photo: PickupPhoto
    caption: str


def photo_caption(photo) -> str:
    """« Photo du 12/03/2031 à 08:14 » when the phone said when it was taken,
    else « Envoyée le … à … »."""
    if photo.taken_at:
        moment = timezone.localtime(photo.taken_at)
        return f"Photo du {moment:%d/%m/%Y} à {moment:%H:%M}"
    moment = timezone.localtime(photo.created_at)
    return f"Envoyée le {moment:%d/%m/%Y} à {moment:%H:%M}"


def shown_photos(pickup) -> list:
    return [ShownPhoto(photo, photo_caption(photo)) for photo in pickup.photos.all()]


def photos_day(photos) -> date | None:
    """The day every photo that knows when it was taken was taken on - None
    when none knows, or they disagree."""
    days = {timezone.localtime(photo.taken_at).date() for photo in photos if photo.taken_at}
    return days.pop() if len(days) == 1 else None


def photos_date_warning(pickup, photos=None) -> tuple:
    """(sentence, the photos' day) when the photos were all taken on another
    day than the pickup's - ("", None) otherwise."""
    day = photos_day(pickup.photos.all() if photos is None else photos)
    if day is None or day == pickup.date:
        return "", None
    return f"Les photos ont été prises le {_day(day)}, la reprise est datée du {_day(pickup.date)}.", day


@dataclass
class CheckPill:
    """An invoice check in a few words: `text` in a pill of its colour (a
    table cell, and « Facture : <pill> » on the cards and the slip's page),
    the whole sentence as its `title`, and `detail`: what the sentence says
    that the pill and what is drawn under it (the invoices, the rows, the
    other credits, the notes) do not - "" when nothing."""

    text: str
    css: str
    title: str
    detail: str = ""


_SHORT = {
    invoice_check.SAME: "remboursé \N{CHECK MARK}",
    invoice_check.DIFFERS: "écart",
    invoice_check.SEVERAL: "à vérifier",
    invoice_check.OTHERS_ONLY: "à vérifier",
    invoice_check.NO_INVOICE: "pas encore reçue",
    invoice_check.NO_TEXT: "sans texte lisible",
    invoice_check.NO_REFERENCE: "pas de référence",
    invoice_check.NO_DATE: "date non lue",
    invoice_check.TOO_SHORT: "référence trop courte",
    invoice_check.SUPERSEDED: "sur le bon qui compte",
}
#: Nothing on the slip, nothing refunded: agreed, but not « remboursé ».
NOTHING_TO_REFUND = "rien à rembourser \N{CHECK MARK}"
#: The states whose pill says all their sentence says: « Facture : pas encore
#: reçue », never « facture facture pas encore reçue ».
_PILL_SAYS_IT = {
    invoice_check.SAME,
    invoice_check.DIFFERS,
    invoice_check.NO_INVOICE,
    invoice_check.NO_TEXT,
    invoice_check.NO_REFERENCE,
    invoice_check.NO_DATE,
    invoice_check.TOO_SHORT,
    invoice_check.SUPERSEDED,
}
#: A state this page has no word for yet (a new one of invoice_check.py)
#: still gets a SHORT pill - it never wraps - from its verdict.
_BY_VERDICT = {True: _SHORT[invoice_check.SAME], False: _SHORT[invoice_check.DIFFERS], None: "à vérifier"}


def check_pill(check) -> CheckPill | None:
    if check is None:
        return None
    if check.state == invoice_check.SAME and not check.rows:
        text = NOTHING_TO_REFUND
    else:
        text = _SHORT.get(check.state) or _BY_VERDICT[check.ok]
    detail = ""
    if check.state not in _PILL_SAYS_IT:
        detail = check.label
        # « … : à vérifier » after an « à vérifier » pill is said twice.
        detail = detail.removesuffix(f" : {text}")
    return CheckPill(text, check.css, check.label, detail)


def format_problem(fmt) -> str:
    """ "" when every pattern of `fmt` still passes the guard, else why not -
    a stored pattern is never trusted (a stricter rule, a hand-edited
    archive)."""
    try:
        patterns.compile_format(fmt, patterns.FORMAT_FIELDS)
    except PatternError as error:
        return error.message
    return ""


@dataclass
class DayView:
    """One pickup's comparison, as its card and page draw it."""

    pickup: Pickup
    day: object
    summary: str
    photos: list
    checks: dict
    date_warning: str = ""
    photos_date: date | None = None
    invoice_checks: list = field(default_factory=list)


def _day_view(board: Board, pickup: Pickup, checks: dict) -> DayView:
    day = board.day(pickup)
    photos = shown_photos(pickup)
    warning, photos_date = photos_date_warning(pickup, [shown.photo for shown in photos])
    # Two slips refunded on one invoice share one check: said once.
    invoice_checks, seen = [], set()
    for info in day.slips:
        check = checks.get(info.pk)
        key = (check.label, tuple(ref.pk for ref in check.invoices)) if check is not None else None
        if check is None or key in seen:
            continue
        seen.add(key)
        invoice_checks.append((info, check, check_pill(check)))
    return DayView(
        pickup=pickup,
        day=day,
        summary=pickup_summary(pickup, board.types),
        photos=photos,
        checks=checks,
        date_warning=warning,
        photos_date=photos_date,
        invoice_checks=invoice_checks,
    )


# -- The pickup form --------------------------------------------------------------------------------------------------


def _form_types(pickup=None) -> list:
    """The types the form counts: the active ones, and - editing - those the
    pickup already counts, active or not."""
    counted = {count.returnable_type_id for count in pickup.counts.all()} if pickup is not None else set()
    return [kind for kind in ReturnableType.objects.order_by("position", "pk") if kind.is_active or kind.pk in counted]


def _store_photos(pickup: Pickup, prepared: list, start: int, saved: list) -> None:
    """Save the prepared photos of `pickup`, numbered after the `start` it
    already has; every name saved goes into `saved` at once, for the caller
    to delete if the transaction fails."""
    for number, photo in enumerate(prepared, start=start + 1):
        row = PickupPhoto(pickup=pickup, taken_at=photo.taken_at, width=photo.width, height=photo.height)
        stem = f"reprise-{pickup.date:%Y%m%d}-{number}"
        row.image.save(f"{stem}.jpg", ContentFile(photo.jpeg), save=False)
        saved.append((row.image.storage, row.image.name))
        row.thumb.save(f"{stem}-vignette.jpg", ContentFile(photo.thumb), save=False)
        saved.append((row.thumb.storage, row.thumb.name))
        row.save()


def _save_pickup(request, form: PickupForm, pickup: Pickup | None = None) -> Pickup | None:
    """Save the pickup `form` describes (a new one when `pickup` is None)
    with the photos of the request, and say what was saved. None when the
    form is refused - its errors are on it, nothing was written."""
    uploads = [upload for upload in request.FILES.getlist(PHOTOS) if getattr(upload, "name", "")]
    if not form.is_valid():
        return None
    existing = list(pickup.photos.all()) if pickup is not None else []
    room = max(0, MAX_PHOTOS - len(existing))
    kept, dropped = uploads[:room], max(0, len(uploads) - room)

    prepared, refused = [], []
    for upload in kept:
        name = clean_text(upload.name, 120) or "photo"
        try:
            prepared.append(prepare_photo(upload))
        except PhotoError as error:
            refused.append(f"Photo non gardée — {name} : {error.message}")

    counts = form.counts()
    if not counts and not prepared and not existing:
        form.add_error(None, NOTHING_TO_SAVE if pickup is None else NOTHING_LEFT)
        for sentence in refused:
            form.add_error(None, sentence)
        return None

    creating = pickup is None
    pickup = pickup or Pickup()
    old_date = pickup.date
    pickup.date = form.cleaned_data["date"]
    pickup.supplier = form.cleaned_data["supplier"]
    pickup.note = (form.cleaned_data.get("note") or "").strip()
    saved = []
    try:
        with transaction.atomic():
            pickup.save()
            if creating:
                PickupCount.objects.bulk_create(
                    PickupCount(pickup=pickup, returnable_type=kind, quantity=counts[kind.pk])
                    for kind in form.types
                    if kind.pk in counts
                )
            else:
                for kind in form.types:
                    if kind.pk in counts:
                        PickupCount.objects.update_or_create(
                            pickup=pickup, returnable_type=kind, defaults={"quantity": counts[kind.pk]}
                        )
                    else:
                        PickupCount.objects.filter(pickup=pickup, returnable_type=kind).delete()
            _store_photos(pickup, prepared, len(existing), saved)
    except BaseException:
        delete_files(saved)
        raise

    what = [f"{kind.name} {counts[kind.pk]}" for kind in form.types if kind.pk in counts]
    if prepared:
        added = "" if creating else (" ajoutées" if len(prepared) > 1 else " ajoutée")
        what.append(_plural(len(prepared), "photo", "photos") + added)
    verb = "enregistrée" if creating else "modifiée"
    head = f"Reprise du {_day(pickup.date)} {verb}"
    if not creating and old_date and old_date != pickup.date:
        head = f"Reprise du {_day(old_date)} {verb} (datée désormais du {_day(pickup.date)})"
    messages.success(request, f"{head} : {', '.join(what)}." if what else f"{head}.")
    if dropped:
        several = dropped > 1
        messages.warning(
            request,
            f"{MAX_PHOTOS} photos au plus par reprise : {dropped} photo{'s' if several else ''} "
            f"envoyée{'s' if several else ''} en trop n'{'ont' if several else 'a'} pas été "
            f"gardée{'s' if several else ''}.",
        )
    for sentence in refused:
        messages.warning(request, sentence)
    warning, _day_of_photos = photos_date_warning(pickup)
    if warning:
        messages.warning(request, warning)
    return pickup


def _existing_same_day(form: PickupForm):
    """The pickup already saved for the day and the supplier the form holds,
    to offer « la compléter » - None when there is none."""
    day = form["date"].value()
    if isinstance(day, str):
        try:
            day = parse_date(day)
        except ValueError:
            day = None
    if not isinstance(day, date):
        return None
    supplier = form["supplier"].value()
    found = Pickup.objects.filter(date=day).prefetch_related("counts__returnable_type").order_by("-pk")
    if supplier in (None, ""):
        found = found.filter(supplier__isnull=True)
    elif is_id(str(supplier)):
        found = found.filter(supplier_id=int(supplier))
    else:
        return None
    return found.first()


def _default_supplier(latest):
    """Who the new pickup's « Repris par » starts on: the latest pickup's,
    else the supplier of the first active format."""
    if latest is not None and latest.supplier_id:
        return latest.supplier_id
    first = (
        SlipFormat.objects.filter(is_active=True).order_by("name", "pk").values_list("supplier_id", flat=True).first()
    )
    return first


# -- /consignes/ ------------------------------------------------------------------------------------------------------


@dataclass
class PickupRow:
    pickup: Pickup
    summary: str
    photo_count: int
    day: object
    slip_labels: list
    checks: list


@dataclass
class SlipRow:
    slip: Slip
    state: object
    line_count: int
    check: CheckPill | None


def home(request):
    today = timezone.localdate()
    latest = Pickup.objects.order_by("-date", "-pk").first()
    types = _form_types()
    if request.method == "POST":
        form = PickupForm(request.POST, types=types, today=today)
        if _save_pickup(request, form) is not None:
            return redirect(f"{_home_url()}?{SAVED_FLAG}=1")
    else:
        form = PickupForm(types=types, today=today, initial={"supplier": _default_supplier(latest)})
    return render(request, "returnables/home.html", _home_context(request, form, today))


def _home_context(request, form: PickupForm, today: date) -> dict:
    show_all = request.GET.get(SHOW_ALL, "")
    pickups_query = (
        Pickup.objects.select_related("supplier").prefetch_related("counts", "photos").order_by("-date", "-pk")
    )
    slips_query = Slip.objects.select_related("format").defer("text").order_by("-received_at", "-pk")
    pickup_total, slip_total = Pickup.objects.count(), Slip.objects.count()
    pickups = list(pickups_query if show_all == "reprises" else pickups_query[:LISTED])
    slip_list = list(slips_query if show_all == "bons" else slips_query[:LISTED])

    board = Board.load(pickups=pickups, slips=slip_list)
    days = [board.day(pickup) for pickup in pickups]
    wanted = {info.pk: info for day in days for info in day.slips}
    wanted.update({slip.pk: slip for slip in slip_list})
    checks = invoice_check.check_many(list(wanted.values()), index=board.index) if wanted else {}

    pickup_rows = [
        PickupRow(
            pickup=pickup,
            summary=pickup_summary(pickup, board.types),
            photo_count=len(pickup.photos.all()),
            day=day,
            slip_labels=[(info.pk, info.label) for info in day.slips],
            checks=[check_pill(checks.get(info.pk)) for info in day.slips],
        )
        for pickup, day in zip(pickups, days, strict=True)
    ]
    slip_rows = [
        SlipRow(
            slip=slip,
            state=board.slip_state(slip),
            line_count=len(board.index.infos[slip.pk].lines or []),
            check=check_pill(checks.get(slip.pk)),
        )
        for slip in slip_list
    ]
    latest_view = _day_view(board, pickups[0], checks) if pickups else None
    formats = list(SlipFormat.objects.select_related("supplier").order_by("name", "pk"))
    active_formats = [fmt for fmt in formats if fmt.is_active]
    broken_formats = [(fmt, problem) for fmt in formats if (problem := format_problem(fmt))]
    same_day = _existing_same_day(form)
    page_messages, upload_messages = _messages_by_place(request)
    return {
        "form": form,
        "today": today,
        "page_messages": page_messages,
        "upload_messages": upload_messages,
        "new_format_tag": NEW_FORMAT,
        "same_day": same_day,
        "same_day_summary": pickup_summary(same_day, board.types or _form_types()) if same_day is not None else "",
        "board": board,
        "latest": latest_view,
        "pickup_rows": pickup_rows,
        "slip_rows": slip_rows,
        "pickup_total": pickup_total,
        "slip_total": slip_total,
        "show_all": show_all,
        # What the lists reload from when a gather ends: the page as it is
        # shown, « tout afficher » included.
        "live_url": _home_url() + (f"?{SHOW_ALL}={show_all}" if show_all in ("reprises", "bons") else ""),
        "listed": LISTED,
        "upload_formats": active_formats,
        "broken_formats": broken_formats,
        "type_errors": board.type_errors,
        "types": board.types,
        "max_photos": MAX_PHOTOS,
        "photo_room": MAX_PHOTOS,
        "saved_flag": SAVED_FLAG,
        "home_url": _home_url(),
        **_gather_context(today),
    }


# -- A pickup ----------------------------------------------------------------------------------------------------------


def pickup_detail(request, pk):
    pickup = get_object_or_404(Pickup.objects.select_related("supplier").prefetch_related("counts", "photos"), pk=pk)
    today = timezone.localdate()
    types = _form_types(pickup)
    if request.method == "POST":
        form = PickupForm(request.POST, types=types, today=today, keep_supplier=pickup.supplier_id)
        edited = Pickup.objects.prefetch_related("photos").get(pk=pickup.pk)
        if _save_pickup(request, form, edited) is not None:
            return redirect("returnables:pickup_detail", pk=pickup.pk)
    else:
        initial = {"date": pickup.date, "supplier": pickup.supplier_id, "note": pickup.note}
        for count in pickup.counts.all():
            initial[f"{COUNT_PREFIX}{count.returnable_type_id}"] = str(count.quantity)
        form = PickupForm(initial=initial, types=types, today=today, keep_supplier=pickup.supplier_id)

    board = Board.load(pickups=[pickup])
    day = board.day(pickup)
    checks = invoice_check.check_many(day.slips, index=board.index) if day.slips else {}
    view = _day_view(board, pickup, checks)
    return render(
        request,
        "returnables/pickup_detail.html",
        {
            "pickup": pickup,
            "view": view,
            "form": form,
            "today": today,
            "type_errors": board.type_errors,
            "max_photos": MAX_PHOTOS,
            "photo_room": max(0, MAX_PHOTOS - len(view.photos)),
            "home_url": _home_url(),
        },
    )


def pickup_delete(request, pk):
    pickup = get_object_or_404(Pickup, pk=pk)
    if request.method != "POST":
        return redirect("returnables:pickup_detail", pk=pickup.pk)
    label = f"Reprise du {_day(pickup.date)}"
    photos = pickup.photos.count()
    delete_with_files(pickup)
    suffix = ", avec sa photo" if photos == 1 else f", avec ses {photos} photos" if photos else ""
    messages.success(request, f"{label} supprimée{suffix}.")
    return redirect("returnables:home")


def pickup_date(request, pk):
    """« Dater la reprise du … », « Mettre la reprise au … »: the date a
    button proposes, checked as the form checks it."""
    pickup = get_object_or_404(Pickup, pk=pk)
    detail = reverse("returnables:pickup_detail", args=[pickup.pk])
    if request.method != "POST":
        return redirect(detail)
    back = _back(request, detail)
    try:
        value = parse_date(request.POST.get("date") or "")
    except ValueError:
        value = None
    problem = check_pickup_date(value)
    if problem:
        messages.error(request, f"{problem} La reprise n'a pas été modifiée.")
        return redirect(back)
    if value == pickup.date:
        messages.info(request, f"La reprise est déjà datée du {_day(value)}.")
        return redirect(back)
    before = pickup.date
    pickup.date = value
    pickup.save(update_fields=["date", "updated_at"])
    messages.success(request, f"Reprise du {_day(before)} mise au {_day(value)}.")
    return redirect(back)


def photo_delete(request, pk):
    photo = get_object_or_404(PickupPhoto, pk=pk)
    detail = reverse("returnables:pickup_detail", args=[photo.pickup_id])
    if request.method != "POST":
        return redirect(detail)
    caption = photo_caption(photo)
    delete_with_files(photo)
    messages.success(request, f"Photo retirée ({caption[0].lower()}{caption[1:]}).")
    return redirect(detail)


# -- The slips --------------------------------------------------------------------------------------------------------


def slip_upload(request):
    """« Ajouter des bons »: every PDF through the one writer
    (slips.store_uploads), said in ONE message."""
    if request.method != "POST":
        return redirect("returnables:home")
    files = request.FILES.getlist(UPLOADS)
    chosen = request.POST.get(UPLOAD_FORMAT, "")
    fmt = None
    if chosen:
        fmt = SlipFormat.objects.filter(pk=int(chosen), is_active=True).first() if is_id(chosen) else None
        if fmt is None:
            messages.error(
                request,
                "Ce format de bon n'existe plus : choisissez-en un dans la liste. Rien n'a été ajouté.",
                extra_tags=UPLOAD_MESSAGES,
            )
            return redirect(f"{_home_url()}#bons")
    if not files:
        messages.error(request, "Aucun fichier choisi : rien n'a été ajouté.", extra_tags=UPLOAD_MESSAGES)
        return redirect(f"{_home_url()}#bons")
    summary = slips.store_uploads(files, fmt)
    level = messages.warning if summary.has_refusals else messages.success
    tags = f"{UPLOAD_MESSAGES} {NEW_FORMAT}" if summary.unrecognised else UPLOAD_MESSAGES
    level(request, summary.message, extra_tags=tags)
    return redirect(f"{_home_url()}#bons")


def slip_detail(request, pk):
    slip = get_object_or_404(Slip.objects.select_related("format__supplier"), pk=pk)
    board = Board.load(slips=[slip])
    state = board.slip_state(slip)
    info = board.index.infos[slip.pk]
    lines = [(line, board.classify(line.designation)) for line in info.lines or []]
    check = invoice_check.check_many([slip], index=board.index).get(slip.pk)
    # The slips this one took the place of (re-sent copies, a slip it cancels).
    replaced_infos = [
        (board.index.infos[other_pk], superseded)
        for other_pk, superseded in board.superseded.items()
        if superseded.by.pk == slip.pk and other_pk in board.index.infos
    ]
    hint_pickups = []
    if state.hint_date is not None:
        hint_pickups = board.units.get((info.supplier_id, state.hint_date), [])
    day_pickups = state.day.pickups if state.day is not None else []
    return render(
        request,
        "returnables/slip_detail.html",
        {
            "slip": slip,
            "info": info,
            "title": slip_label(slip.number, slip.delivery_date, with_date=True, capital=True),
            "state": state,
            "lines": lines,
            "types": board.types,
            "check": check,
            "check_pill": check_pill(check),
            "replaced": replaced_infos,
            "hint_pickups": hint_pickups,
            "day_pickups": day_pickups,
            "format_problem": format_problem(slip.format),
            "type_errors": board.type_errors,
            "home_url": _home_url(),
        },
    )


def slip_delete(request, pk):
    slip = get_object_or_404(Slip, pk=pk)
    if request.method != "POST":
        return redirect("returnables:slip_detail", pk=slip.pk)
    label = slip_label(slip.number, slip.delivery_date, with_date=True, capital=True)
    delete_with_files(slip)
    messages.success(request, f"{label} supprimé.")
    return redirect("returnables:home")


def _reading_said(result) -> str:
    if result.error:
        return f"il n'a pas pu être lu : {result.error.rstrip('.')}"
    count = len(result.lines)
    said = f"{count} ligne lue" if count <= 1 else f"{count} lignes lues"
    failed = [check.label for check in result.failed_checks]
    if failed:
        said += f" ; à vérifier : {', '.join(failed)}"
    return said


def slip_reread(request, pk):
    slip = get_object_or_404(Slip.objects.select_related("format"), pk=pk)
    if request.method != "POST":
        return redirect("returnables:slip_detail", pk=slip.pk)
    result = slips.reread(slip)
    label = slip_label(slip.number, slip.delivery_date, with_date=True, capital=True)
    (messages.warning if result.error or result.failed_checks else messages.success)(
        request, f"{label} relu avec les motifs actuels : {_reading_said(result)}."
    )
    return redirect("returnables:slip_detail", pk=slip.pk)


def line_classify(request, pk):
    """« Classer comme »: `^` and the line's designation (its first 60
    characters, escaped - spaces kept readable) added to the chosen type's
    patterns, the whole field checked again before it is saved."""
    line = get_object_or_404(SlipLine, pk=pk)
    detail = reverse("returnables:slip_detail", args=[line.slip_id])
    if request.method != "POST":
        return redirect(detail)
    posted = request.POST.get("type", "")
    kind = ReturnableType.objects.filter(pk=int(posted)).first() if is_id(posted) else None
    if kind is None:
        messages.error(request, "Choisissez un type de consigne existant : rien n'a été modifié.")
        return redirect(detail)
    pattern = "^" + regex.escape(line.designation[:60], literal_spaces=True)
    existing = [text.strip() for text in (kind.slip_patterns or "").splitlines() if text.strip()]
    if pattern in existing:
        messages.info(request, f"Le motif « {pattern} » est déjà parmi ceux du type « {kind.name} ».")
        return redirect(detail)
    value = "\n".join([*existing, pattern])
    try:
        patterns.compile_field(patterns.TYPE_FIELD, value)
    except PatternError as error:
        messages.error(request, f"{error.message} Rien n'a été modifié.")
        return redirect(detail)
    kind.slip_patterns = value
    kind.save(update_fields=["slip_patterns"])
    now = comparison.Classifier(ReturnableType.objects.all()).classify(line.designation)
    if now.returnable_type is not None and now.returnable_type.pk != kind.pk:
        messages.warning(
            request,
            f"Motif « {pattern} » ajouté au type « {kind.name} », mais le type « {now.returnable_type.name} », placé "
            "avant lui, reconnaît déjà cette ligne : changez l'ordre des types.",
        )
    else:
        messages.success(request, f"Motif « {pattern} » ajouté au type « {kind.name} » : la ligne est de ce type.")
    return redirect(detail)


# -- Slip formats -------------------------------------------------------------------------------------------------------


def format_list(request):
    formats = list(
        SlipFormat.objects.select_related("supplier").annotate(slip_count=Count("slips")).order_by("name", "pk")
    )
    return render(
        request,
        "returnables/format_list.html",
        {"rows": [(fmt, format_problem(fmt)) for fmt in formats], "home_url": _home_url()},
    )


def format_create(request):
    return _format_page(request, None)


def format_edit(request, pk):
    return _format_page(request, get_object_or_404(SlipFormat.objects.select_related("supplier"), pk=pk))


def _duplicate_of(request):
    posted = request.GET.get("depuis", "")
    if not is_id(posted):
        return None
    return SlipFormat.objects.filter(pk=int(posted)).first()


def _copy_initial(source: SlipFormat) -> dict:
    """« Dupliquer »: every pattern of `source`, a name of its own, no
    supplier (a copy is for another seller)."""
    initial = {name: getattr(source, name) for name in SlipFormatForm.Meta.fields if name not in ("name", "supplier")}
    initial["name"] = f"Copie de {source.name}"[:80]
    initial["is_active"] = True
    return initial


@dataclass
class TestResult:
    """What « Tester » shows: the trace of the unsaved patterns on a text, or
    why there is nothing to trace."""

    source: str = ""
    trace: object = None
    problem: str = ""

    @property
    def lines(self) -> list:
        """The trace's lines the page draws: the first TRACE_LINES_SHOWN (a
        pasted text can hold thousands)."""
        return list(self.trace.lines[:TRACE_LINES_SHOWN]) if self.trace is not None else []

    @property
    def more(self) -> int:
        return max(0, len(self.trace.lines) - TRACE_LINES_SHOWN) if self.trace is not None else 0


def _unsaved_patterns(request) -> SimpleNamespace:
    """The patterns as posted, not as saved: what « Tester » reads with."""
    return SimpleNamespace(**{field.attr: request.POST.get(field.attr, "") for field in patterns.FORMAT_FIELDS})


def _test(request, fmt) -> tuple:
    """(TestResult, the text tested - written back into the paste box, so a
    PDF is read once)."""
    uploaded = request.FILES.get(TEST_PDF)
    chosen = request.POST.get(TEST_SLIP, "")
    text = request.POST.get(TEST_TEXT, "") or ""
    source = "le texte collé"
    if uploaded is not None:
        name = clean_text(uploaded.name, 120) or "document"
        if uploaded.size is not None and uploaded.size > reading.MAX_PDF_BYTES:
            return TestResult(problem=f"{name} : {reading.TOO_HEAVY}"), text
        try:
            text = slips.document_text(reading.pdf_text(uploaded.read()))
        except SlipError as error:
            return TestResult(problem=f"{name} : {error.message}"), text
        source = f"le PDF « {name} »"
    elif chosen and fmt is not None:
        slip = (
            fmt.slips.filter(pk=int(chosen)).only("pk", "format", "text", "number", "delivery_date").first()
            if is_id(chosen)
            else None
        )
        if slip is None:
            return TestResult(problem="Ce bon n'existe plus : choisissez-en un autre."), text
        text = slip.text or ""
        source = f"le {slip_label(slip.number, slip.delivery_date, with_date=True)}"
    if not text.strip():
        return TestResult(problem="Rien à tester : choisissez un bon reçu, un PDF, ou collez le texte d'un bon."), text
    return TestResult(source=source, trace=reading.trace(text, _unsaved_patterns(request))), text


def _format_page(request, fmt):
    source = _duplicate_of(request) if fmt is None else None
    result, tested_text = None, ""
    if request.method == "POST":
        action = request.POST.get(ACTION)
        instance = SlipFormat.objects.get(pk=fmt.pk) if fmt is not None else SlipFormat()
        form = SlipFormatForm(request.POST, instance=instance)
        if action == TEST:
            result, tested_text = _test(request, fmt)
            if _is_htmx(request):
                return render(
                    request,
                    "returnables/_format_test.html",
                    {
                        "result": result,
                        "fmt": fmt,
                        "tested_text": tested_text,
                        "test_slips": _test_slips(fmt),
                        "oob": True,
                    },
                )
            form.is_valid()
        elif action == SAVE:
            if form.is_valid():
                saved = form.save()
                count = saved.slips.count()
                said = f"Format « {saved.name} » enregistré."
                if count == 1:
                    said += " Son bon n'a pas été relu : « Relire » le lit avec ces motifs."
                elif count:
                    said += f" Ses {count} bons n'ont pas été relus : « Relire » les lit avec ces motifs."
                messages.success(request, said)
                return redirect("returnables:format_edit", pk=saved.pk)
        else:
            messages.error(request, UNKNOWN_ACTION)
            return redirect(request.path)
    elif source is not None:
        form = SlipFormatForm(initial=_copy_initial(source))
    else:
        form = SlipFormatForm(instance=fmt)
    return render(
        request,
        "returnables/format_form.html",
        {
            "fmt": fmt,
            "source": source,
            "form": form,
            "result": result,
            "tested_text": tested_text,
            "test_slips": _test_slips(fmt),
            "slip_count": fmt.slips.count() if fmt is not None else 0,
            "format_problem": format_problem(fmt) if fmt is not None and request.method != "POST" else "",
            "uba_line": UBA_EXAMPLE_LINE,
            "uba_pattern": UBA_EXAMPLE_PATTERN,
            "home_url": _home_url(),
        },
    )


#: The cheat-sheet's worked example: the seeded UBA line pattern on an
#: INVENTED line.
UBA_EXAMPLE_PATTERN = (
    r"^(?P<designation>.+?)\s+(?P<quantite>-?\d+)\s+x\s+(?P<prix>-?\d+(?:[.,]\d+)?)\s+=\s+"
    r"(?P<montant>-?\d+(?:[.,]\d+)?)\s*$"
)
UBA_EXAMPLE_LINE = "FÛT EXEMPLE 30 L 4 x 30.00 = 120.00"


def _test_slips(fmt) -> list:
    if fmt is None:
        return []
    return [
        (slip.pk, slip_label(slip.number, slip.delivery_date, with_date=True, capital=True), slip.original_name)
        # `format` in only(): through `fmt.slips`, Django sets each slip's
        # format from its format_id - deferred, that is one query per slip.
        for slip in fmt.slips.only("pk", "format", "number", "delivery_date", "original_name").order_by(
            "-received_at", "-pk"
        )[:TEST_SLIPS_OFFERED]
    ]


def format_delete(request, pk):
    fmt = get_object_or_404(SlipFormat, pk=pk)
    if request.method != "POST":
        return redirect("returnables:format_edit", pk=fmt.pk)
    name = fmt.name
    try:
        delete_with_files(fmt)
    except ProtectedError:
        count = fmt.slips.count()
        messages.error(
            request,
            f"Le format « {name} » a {_plural(count, 'bon', 'bons')} : il ne peut pas être supprimé - désactivez-le "
            "plutôt.",
        )
        return redirect("returnables:format_edit", pk=fmt.pk)
    messages.success(request, f"Format « {name} » supprimé.")
    return redirect("returnables:format_list")


def format_reread(request, pk):
    fmt = get_object_or_404(SlipFormat, pk=pk)
    if request.method != "POST":
        return redirect("returnables:format_edit", pk=fmt.pk)
    done, left = slips.reread_format(fmt)
    if left:
        messages.warning(
            request,
            f"{_plural(done, 'bon relu', 'bons relus')} avec les motifs actuels ; il en reste {left} : "
            "« Relire » à nouveau pour les lire.",
        )
    else:
        messages.success(request, f"{_plural(done, 'bon relu', 'bons relus')} avec les motifs actuels.")
    return redirect("returnables:format_edit", pk=fmt.pk)


# -- Returnable types -------------------------------------------------------------------------------------------------


def _type_page(request, bound=None, new_form=None, status=200):
    types = list(ReturnableType.objects.annotate(count_rows=Count("counts")).order_by("position", "pk"))
    classifier = comparison.Classifier(types)
    rows = []
    for kind in types:
        form = (
            bound
            if bound is not None and bound.instance.pk == kind.pk
            else TypeForm(instance=kind, prefix=f"type-{kind.pk}")
        )
        rows.append((kind, form, classifier.errors.get(kind.pk, "")))
    if new_form is None:
        next_position = (max((kind.position for kind in types), default=0) + 1) if types else 1
        new_form = TypeForm(prefix="nouveau", initial={"position": next_position, "is_active": True})
    return render(
        request,
        "returnables/type_list.html",
        {"rows": rows, "new_form": new_form, "home_url": _home_url()},
        status=status,
    )


def type_list(request):
    if request.method == "POST":
        form = TypeForm(request.POST, prefix="nouveau")
        if form.is_valid():
            kind = form.save()
            messages.success(request, f"Type « {kind.name} » ajouté.")
            return redirect("returnables:type_list")
        return _type_page(request, new_form=form)
    return _type_page(request)


def type_edit(request, pk):
    kind = get_object_or_404(ReturnableType, pk=pk)
    if request.method != "POST":
        return redirect(f"{reverse('returnables:type_list')}#type-{kind.pk}")
    form = TypeForm(request.POST, instance=ReturnableType.objects.get(pk=kind.pk), prefix=f"type-{kind.pk}")
    if form.is_valid():
        saved = form.save()
        messages.success(request, f"Type « {saved.name} » enregistré.")
        return redirect(f"{reverse('returnables:type_list')}#type-{saved.pk}")
    return _type_page(request, bound=form)


def type_delete(request, pk):
    kind = get_object_or_404(ReturnableType, pk=pk)
    if request.method != "POST":
        return redirect(f"{reverse('returnables:type_list')}#type-{kind.pk}")
    name = kind.name
    try:
        delete_with_files(kind)
    except ProtectedError:
        messages.error(
            request,
            f"Le type « {name} » est compté dans des reprises : il ne peut pas être supprimé - désactivez-le plutôt.",
        )
        return redirect(f"{reverse('returnables:type_list')}#type-{kind.pk}")
    messages.success(request, f"Type « {name} » supprimé.")
    return redirect("returnables:type_list")
