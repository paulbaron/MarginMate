"""Small shared form helpers.

Project-level rather than per-app because the problem below has now bitten
three different formsets across three different apps.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date


def is_id(value) -> bool:
    """Whether `value`, read from a request, is an id: ASCII digits only.
    str.isdigit() also says yes to "²" or "٣" - and "²" is no int, so the
    query it reached raised: a 500 where a tampered form gets a message."""
    # And no longer than an SQLite integer can be: past it, the query
    # raised OverflowError.
    return isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18


def local_path(request, target) -> str:
    """`target` when it is a path of this site - « /banque/?mois=2026-07 » -
    and "" for anything else.

    Three things are never followed. Another site (« https://… »,
    « //ailleurs.example », « /\\ailleurs.example », which a browser reads
    as « //… »): an open redirect is a link anybody can send to the owner,
    landing wherever they chose. Anything that does not start with « / »
    (« abc », « invoices:invoice_list »): Django's
    url_has_allowed_host_and_scheme accepts it as a relative address, then
    redirect() takes it for the NAME of a route, finds none and raises
    NoReverseMatch - a 500 for anybody who edited the address (security
    audit LB-5). And a control character: « /a%0Ab » passes that check
    (urlsplit drops the line break) and then fails as a header, another
    500."""
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in target):
        return ""
    from django.utils.http import url_has_allowed_host_and_scheme

    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return ""


def safe_next(request, default: str, param: str = "next") -> str:
    """Where `next` (posted, or in the address) asks to go back to - a path
    of this site (`local_path`) - or `default`, a ready address. The one
    rule behind every « back to the page that asked » (Achats' deletions,
    the bank's actions, the margins' panel)."""
    target = request.POST.get(param) or request.GET.get(param) or ""
    return local_path(request, target) or default


def local_return(request) -> str:
    """Where `retour` (posted, or in the address) asks to go back to - a path
    of this site only (`local_path`), "" otherwise. A page posting to another
    app's route (« Consignes » to Achats' « Récupérer ») comes back to itself
    through it."""
    return local_path(request, request.POST.get("retour") or request.GET.get("retour") or "")


# -- What an upload may weigh (security audit UPLOAD-1) ----------------------------------------------------------

MEGABYTE = 1024 * 1024
#: One file sent through any of the app's forms - a ticket's photo or scan, a
#: supplier's PDF, a bank statement, a hand-typed invoice's receipt. A phone
#: photo weighs 2 to 8 MB, a scanned ticket 0.3 MB (the owner's folder of 42:
#: 0.74 MB at most), a supplier's PDF a few hundred KB: 25 MB is three times
#: the heaviest of them and still refuses what is no document at all (a 64 MB
#: file named .jpg was staged whole, audit UPLOAD-1). « Données » archives
#: have their own, larger cap (transfer/archive.py MAX_ARCHIVE_BYTES).
UPLOAD_MAX_FILE_BYTES = 25 * MEGABYTE
#: Everything one form sends at once, when it takes several files (the bank's
#: statements). The folder of tickets has its own, larger one
#: (invoices.forms.RECEIPT_BATCH_MAX_BYTES).
UPLOAD_MAX_TOTAL_BYTES = 100 * MEGABYTE
#: What ONE request may carry online. Cloudflare's free plan refuses a body
#: over 100 MB with its own English page before the server sees it, and a
#: browser never gives back the photos of a refused post: they are lost. So
#: a page that piles up camera shots in one form (Achats' « Prendre une
#: photo », static/js/photos.js's data-max-bytes) stops short of it, with
#: room left for the multipart framing. The server does NOT enforce it: a
#: folder imported from the PC itself (up to forms.RECEIPT_BATCH_MAX_BYTES,
#: 500 MB) never goes through Cloudflare and stays legitimate.
ONLINE_SEND_MAX_BYTES = 90 * MEGABYTE

FILE_TOO_BIG = "« {name} » pèse {size} : {limit} au plus par fichier."
SELECTION_TOO_BIG = "La sélection pèse {size} : {limit} au plus en une fois. Envoyez-la en plusieurs fois."


def weight(size: int) -> str:
    """« 25 Mo », « 612,4 Mo », « 300 Ko » - a file's weight as a page says it."""
    size = max(int(size or 0), 0)
    if size < MEGABYTE:
        return f"{max(-(-size // 1024), 1) if size else 0} Ko"
    text = f"{size / MEGABYTE:.1f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} Mo"


def upload_size(upload) -> int:
    """What an uploaded file weighs (Django counts it as it arrives)."""
    size = getattr(upload, "size", None)
    return size if isinstance(size, int) and size >= 0 else 0


def file_too_big(upload, limit: int | None = None) -> str:
    """The sentence refusing `upload` by its name when it weighs more than
    `limit` (UPLOAD_MAX_FILE_BYTES, read at the call), "" when it does not."""
    limit = UPLOAD_MAX_FILE_BYTES if limit is None else limit
    size = upload_size(upload)
    if size <= limit:
        return ""
    return FILE_TOO_BIG.format(name=getattr(upload, "name", "") or "fichier", size=weight(size), limit=weight(limit))


def selection_too_big(uploads, limit: int | None = None) -> str:
    """The sentence refusing a whole selection heavier than `limit`
    (UPLOAD_MAX_TOTAL_BYTES, read at the call), "" when it is not."""
    limit = UPLOAD_MAX_TOTAL_BYTES if limit is None else limit
    total = sum(upload_size(upload) for upload in uploads)
    if total <= limit:
        return ""
    return SELECTION_TOO_BIG.format(size=weight(total), limit=weight(limit))


# -- What a page may say of an error (security audit LB-3) --------------------------------------------------------

#: Said of an error that is none of the app's own refusals. Its own words - a
#: library's, in English, with a file's path on the server: PIL's « cannot
#: identify image file '<TENANTS_ROOT>\\<tenant>\\imports\\…' » was drawn on
#: the batch page of whichever bar sent a broken photo - go to the server's
#: log, never to a page.
SERVER_ERROR = "Erreur inattendue sur le serveur : elle est notée pour l'administrateur."
UNREADABLE_IMAGE = "Image illisible : le fichier est abîmé, ou ce n'est pas une image."
UNREADABLE_PDF = "PDF illisible : le fichier est abîmé, ou ce n'est pas un PDF."

_IMAGE_LIBRARIES = ("PIL",)
_PDF_LIBRARIES = ("pypdfium2", "pdfminer", "pdfplumber")


def _raised_in(exc: BaseException) -> str:
    """The module the exception was raised in (its innermost Python frame)."""
    trace, module = exc.__traceback__, ""
    while trace is not None:
        module = trace.tb_frame.f_globals.get("__name__", "") or ""
        trace = trace.tb_next
    return module


def _from(module: str, packages) -> bool:
    return any(module == package or module.startswith(package + ".") for package in packages)


def error_kind(exc: BaseException) -> str:
    """The fixed sentence for an error that is not the app's own: an image or
    a PDF its library could not read, anything else the server's."""
    module = _raised_in(exc)
    try:
        from PIL import Image, UnidentifiedImageError

        if isinstance(exc, (UnidentifiedImageError, Image.DecompressionBombError)):
            return UNREADABLE_IMAGE
    except ImportError:  # pragma: no cover - Pillow is a requirement
        pass
    try:
        from pypdfium2 import PdfiumError

        if isinstance(exc, PdfiumError):
            return UNREADABLE_PDF
    except ImportError:  # pragma: no cover - pypdfium2 is a requirement
        pass
    if _from(module, _IMAGE_LIBRARIES):
        return UNREADABLE_IMAGE
    if _from(module, _PDF_LIBRARIES):
        return UNREADABLE_PDF
    return SERVER_ERROR


def error_for_page(exc: BaseException, *, said=(), log=None, what: str = "") -> str:
    """What a page a bar reads may say of `exc`.

    Its own words only when it is one of `said` - the app's refusals, written
    in French for the person (an electronic invoice refused, a document too
    long to read). Anything else is a fixed sentence by kind (`error_kind`),
    and its detail - message and traceback - goes to `log` (a logger), under
    `what`: the server's log is where a path may be written."""
    if said and isinstance(exc, said):
        return str(exc).strip() or SERVER_ERROR
    kind = error_kind(exc)
    if log is not None:
        log.error("%s - montré comme « %s »", what or "Erreur", kind, exc_info=(type(exc), exc, exc.__traceback__))
    return kind


def search_key(text: str) -> str:
    """A comparison key that ignores case AND accents - « biere » has to find
    « Bière », since nobody reaches for the compose key while typing fast at
    a bar.

    Here rather than in one app because three search boxes now need the same
    answer (the articles, the documents, the box that attaches a document to
    a payment), and static/js/datatable.js normalises the same way for every
    table it draws. SQLite's own `icontains` folds case but not accents, so
    this runs in Python over the few rows that can be compared that way - a
    supplier list, never a list of documents.
    """
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(letter for letter in decomposed if not unicodedata.combining(letter)).lower()


def plain_number(value) -> str:
    """A decimal as a person writes it: 0.82, 2, 10, -1 - never "0.820",
    nor the "1E+1" Decimal.normalize() makes of 10. "" for nothing."""
    if value is None or value == "":
        return ""
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return str(number.quantize(Decimal("1")))
    return format(number.normalize(), "f")


#: The colours the inline SVG charts give their slices, in order. There is
#: no charting library here (see recipes/views.py::_build_ingredient_pie_svg),
#: so the palette is data, and it lives in ONE place: two pies in one app
#: drawn from two lists that drifted apart read as two different legends.
#:
#: Sixteen, not eight, since « Dépenses » stopped folding its tail at the
#: palette's length and started folding it at a share (bank.spending
#: SMALLEST_SLICE): a page with a dozen real categories then had two wedges
#: of the same colour, which is a legend that lies. All light enough to read
#: on the dark panel, and ordered so neighbours differ in hue rather than in
#: shade alone.
PIE_COLORS = [
    "#d99b3f",
    "#6fbf73",
    "#e0685f",
    "#5b9bd9",
    "#c77dd9",
    "#d9c73f",
    "#3fd9c7",
    "#9fa2ae",
    "#e89a6a",
    "#8fd94f",
    "#d95f9b",
    "#4fb8d9",
    "#9b7de0",
    "#bfae6a",
    "#5fd99b",
    "#c9c9d6",
]

#: The two query parameters every page reads its window from - « du » and
#: « au », as the pages say it. One pair of names, so a window survives
#: being carried from a link on one page to another.
RANGE_START, RANGE_END = "du", "au"

#: How long « les douze derniers mois » is, for the pages whose period has a
#: default - « Marges » and « Dépenses ». One number: the two say the same
#: words on screen, and two pages disagreeing about what that phrase covers
#: is a figure nobody can reconcile with the other page's.
DEFAULT_WINDOW_DAYS = 365


@dataclass(frozen=True)
class DateRange:
    """A window a person typed: « du 01/02/2026 au 28/02/2026 ».

    **Both ends are included.** A person asking for the 28th means the 28th,
    and a document dated that day is in. This is deliberately NOT the
    half-open window the stock pages slice with (variance.movements_between,
    recipes.sales.sales_between): there, the opening date belongs to the
    count taken that day, so a delivery on it is already counted. Here there
    is no count - only two dates and what falls between them.

    Either end may be missing: « depuis le 1er février » and « jusqu'au 28 »
    are both windows a person asks for, and neither is an error.
    """

    start: date | None = None
    end: date | None = None

    def __bool__(self) -> bool:
        return self.start is not None or self.end is not None

    def limit(self, queryset, field: str):
        """`queryset` narrowed to the window on `field`.

        A row whose date is NULL drops out as soon as either end is set: an
        undated document is in no window (« Sans date » is where those are
        looked at). `__gte`/`__lte` already exclude NULL in SQL; this is
        said here because it is a decision, not an accident.
        """
        if self.start is not None:
            queryset = queryset.filter(**{f"{field}__gte": self.start})
        if self.end is not None:
            queryset = queryset.filter(**{f"{field}__lte": self.end})
        return queryset

    def holds(self, day: date | None) -> bool:
        """Whether `day` is in the window - for what is summed in Python
        rather than filtered in SQL (a stock movement's effective_date).

        An undated row is never held, the way `limit` drops it; an empty
        window holds everything, the way `limit` filters nothing.
        """
        if day is None:
            return not self
        if self.start is not None and day < self.start:
            return False
        return not (self.end is not None and day > self.end)

    @property
    def start_value(self) -> str:
        """What to put in `value=""` of an `<input type="date">`."""
        return self.start.isoformat() if self.start else ""

    @property
    def end_value(self) -> str:
        return self.end.isoformat() if self.end else ""

    @property
    def parameters(self) -> dict:
        """The window as query parameters, to hang on a link that must keep
        it - a filter chip, a tab. Empty when there is no window."""
        return {key: value for key, value in ((RANGE_START, self.start_value), (RANGE_END, self.end_value)) if value}


def read_date(value) -> date | None:
    """One date read from a request, or None for anything else.

    `<input type="date">` posts ISO 8601, which is all this reads. Anything
    else - a stale bookmark, a hand-typed URL, a browser with no date input
    where someone wrote "hier" - is no window rather than a 500: parse_date
    returns None on a shape it doesn't know and RAISES on a shape it does
    know that is no date ("2026-02-30"), and both mean the same thing here.
    """
    try:
        return parse_date((value or "").strip())
    except (ValueError, TypeError):
        return None


def last_twelve_months(today=None) -> DateRange:
    """« Les douze derniers mois », the default period of the pages that
    have one.

    An all-time figure on those pages mixes three years of purchase prices
    with three years of selling prices and means very little, so they open
    on a period and say on screen which one it is. Here rather than in each
    view because two pages naming the same period and counting two different
    spans is exactly the kind of quiet disagreement this codebase keeps
    paying for.
    """
    today = today or timezone.localdate()
    return DateRange(today - timedelta(days=DEFAULT_WINDOW_DAYS), today)


def date_range(request, start_param: str = RANGE_START, end_param: str = RANGE_END) -> DateRange:
    """The window `?du=&au=` asks for, empty when it asks for none.

    Two dates the wrong way round are swapped rather than answered with an
    empty page: « du 28/02 au 01/02 » is a typo, and what the person means
    by it is unambiguous.
    """
    start = read_date(request.GET.get(start_param))
    end = read_date(request.GET.get(end_param))
    if start is not None and end is not None and start > end:
        start, end = end, start
    return DateRange(start, end)


#: « sans … »: what a page leaves out of one of its figures, as a VIEW carried
#: in the address like the period - repeated, no model, no migration - so two
#: tabs hold two questions and a bookmark keeps its own. « Marges » leaves
#: spending out of a second real margin, « Dépenses » categories out of its
#: pie; one spelling for both, since a reader carries the habit from one page
#: to the other.
LEFT_OUT_PARAM = "sans"
#: What a « Recalculer » form sends. A checkbox that is not ticked sends
#: NOTHING, so « left out » cannot be read off what came back alone: every
#: row the table showed sends its key under `montre`, and the boxes still
#: ticked send it again under `garder`.
SHOWN_PARAM = "montre"
KEPT_PARAM = "garder"


def left_out_from(query, key=None) -> list[str]:
    """What a « Recalculer » leaves out: the rows the form showed and did not
    send back ticked, after what was left out already and not on the form.

    `query` is the request's GET (a QueryDict: `montre`, `garder` and `sans`
    all repeat). `key` is what two spellings of one row are compared by - a
    page whose rows are free text passes its own cleaning, so « TVA » in the
    address and « TVA » on the form cannot be two rows; the default compares
    them as sent. What comes back is still the page's to check against what
    it knows: this only reads the form.

    Read as « shown and not kept », never as « not sent »: an unticked box
    sends nothing, and so does a row a stale page never had - a newer
    invoice's article, a category first named since - which read as unticked
    would drop out with nobody having touched it. A `garder` for a row the
    form never showed changes nothing either, and a key the form never
    showed (nothing under it in the window) keeps the state it had.

    In the order they were ASKED, what is newly left out after: in table
    order, a « Recalculer » that changed nothing turned « sans : Matériel,
    Rhum » into « sans : Rhum, Matériel ». One definition for every page that
    has such a form - « Marges » had it alone, and a rule with two
    definitions is this codebase's oldest sin.
    """
    same = key or (lambda value: value)
    shown = [same(value) for value in query.getlist(SHOWN_PARAM)]
    kept = {same(value) for value in query.getlist(KEPT_PARAM)}
    before = [same(value) for value in query.getlist(LEFT_OUT_PARAM)]
    on_the_form = set(shown)
    unticked = {one for one in shown if one not in kept}
    still_out = [one for one in before if one not in on_the_form or one in unticked]
    return still_out + [one for one in shown if one in unticked and one not in before]


class BlankRowTolerantFormMixin:
    """Makes a formset row count as blank unless the user actually filled
    something in, ignoring fields that only carry defaults or bookkeeping.

    Django only skips validating an extra formset row when its
    ``has_changed()`` is False, and ``has_changed()`` is True as soon as ANY
    field differs from its initial. That breaks two things at once:

    * A row removed client-side leaves a GAP in the posted indices (0, 1, 3
      with TOTAL_FORMS=4) - index 2 is absent from the POST entirely. A field
      declared with an ``initial`` then reads as "changed" (initial 0 vs
      nothing submitted), so the invisible row gets validated and fails with
      "this field is required" on a row the user cannot see or fix.
    * A row left at its pre-filled default (a VAT rate of 20%) reads as
      changed too, so an untouched trailing row blocks the save.

    Both are invisible from a happy-path test, because the browser's own
    payloads - non-contiguous indices, defaults echoed back - are not what a
    hand-written test naturally posts. See recipes/tests/test_forms.py.

    Subclasses list the field names that don't count as user input.
    """

    #: Fields that carry a default or bookkeeping value rather than a real
    #: user entry, and so must not by themselves make a row "filled in".
    bookkeeping_fields: tuple[str, ...] = ()

    def has_changed(self) -> bool:
        # Django asks this question for two different reasons, and only one
        # of them wants the bookkeeping fields ignored:
        #
        #   1. "May I skip validating this blank row?" - asked only of rows
        #      that are allowed to be blank (empty_permitted), i.e. the extra
        #      ones. That's the question this mixin exists to answer.
        #   2. "Has this SAVED row changed, so should I write it back?" -
        #      asked by BaseModelFormSet.save_existing_objects of every
        #      initial row, which never has empty_permitted set.
        #
        # Answering (2) with the bookkeeping fields stripped out silently
        # discards real edits: regrouping an existing ingredient via the "OU"
        # button changes nothing BUT `group`, so the save became a no-op and
        # the recipe reopened with the old grouping. Hence the guard.
        if not self.empty_permitted:
            return super().has_changed()
        return bool(set(self.changed_data) - set(self.bookkeeping_fields))


class BlankRowTolerantForm(BlankRowTolerantFormMixin, forms.Form):
    pass


class BlankRowTolerantModelForm(BlankRowTolerantFormMixin, forms.ModelForm):
    pass


class JobLogMixin(models.Model):
    """Everything a background job needs beyond its own fields.

    Both job models append timestamped lines to one text field, and both are
    driven by a daemon thread that cannot be relied on to reach its own
    `finally`: the dev server's autoreloader kills it outright on any code
    change. The job is then left RUNNING for ever, which blocks every future
    run behind "already in progress" - and its Cancel button does nothing,
    because there is no thread left to notice. That combination is a deadlock
    with no way out from the UI, and it is what stopped a three-year sales
    import from ever starting again.

    `last_heartbeat` is how a run says "still here" during the long silent
    stretches; anything quiet for STALE_AFTER is presumed dead and reaped.
    """

    #: Deliberately generous: one export of three years of tickets takes
    #: about a minute to generate and fetch, and declaring a working job dead
    #: is worse than waiting a little longer.
    STALE_AFTER = timedelta(minutes=10)

    last_heartbeat = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    #: "[+  12.3s] " - real information while a job runs, noise on the one
    #: line shown as a live status, where the spinner already says "running".
    _ELAPSED_PREFIX = re.compile(r"^\[\+\s*[\d.]+s\]\s*")

    def beat(self) -> None:
        """Say "still alive" without writing a log line - the download has
        long silent stretches, and a line every two seconds would bury the
        log it shares."""
        self.last_heartbeat = timezone.now()
        self.save(update_fields=["last_heartbeat"])

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.RUNNING)

    @property
    def is_stale(self) -> bool:
        """Nominally running, but nothing has been heard from it."""
        if not self.is_active:
            return False
        since = self.last_heartbeat or self.started_at
        return timezone.now() - since > self.STALE_AFTER

    @classmethod
    def reap_stale(cls) -> int:
        """Mark abandoned runs as failed, so they stop blocking new ones."""
        stale = [job for job in cls.objects.filter(status__in=[cls.Status.PENDING, cls.Status.RUNNING]) if job.is_stale]
        for job in stale:
            job.status = cls.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at"])
            job.append_log(
                "Interrompue : plus aucune nouvelle de cette exécution. "
                "Le serveur a probablement redémarré pendant qu'elle tournait."
            )
        return len(stale)

    @property
    def log_lines(self) -> int:
        return len([line for line in (self.log or "").splitlines() if line.strip()])

    @property
    def last_log_line(self) -> str:
        for line in reversed((self.log or "").splitlines()):
            if line.strip():
                return self._ELAPSED_PREFIX.sub("", line).strip()
        return ""
