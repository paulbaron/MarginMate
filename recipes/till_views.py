"""« Formats des fichiers de caisse »: how a bar's own till export is read.

Two pages - the formats and a new one (`till_formats`), one format
(`till_format`) - each with « Tester »: a file picked on the page read with
the format AS TYPED, nothing saved, the file never kept (read in memory and
dropped), at most `TEST_ROW_LIMIT` rows. The owner's alone
(accounts/access.py): a format decides what an upload writes into the till's
sales and payments, which « Entrées d'argent » holds against the bank.

Modelled on the bank's « Format du relevé » (bank/views.py): « Tester » is
the forms' first submit button, so Enter tests and never saves; a refusal is
said on its field (`till_file.check_format`, through the form).
"""

from __future__ import annotations

import io
import threading
import uuid
from dataclasses import dataclass, field

from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst

from accounts.tenancy import bound
from common import file_too_big, read_date

from .forms import TillFormatForm
from .models import PosDailyPayment, PosProduct, TillFormat
from .payments import by_method
from .pos import till_file

#: The file « Tester » reads, posted with the form, and the day it reads a
#: format with no day column at (a daily report).
TEST_FILE = "fichier_essai"
TEST_DAY = "jour_essai"
#: What « Tester » shows of the file: its first rows split into numbered
#: columns; and how far it reads before it stops (a request is no job).
TEST_ROWS_SHOWN = 15
TEST_ROW_LIMIT = 5000
TEST_PRODUCTS_SHOWN = 20
ACTION = "action"
TEST, SAVE, DELETE = "tester", "enregistrer", "supprimer"
NO_TEST_FILE = "Choisissez un fichier pour voir ce que le format en lit."
UNKNOWN_ACTION = "Action inconnue : rien n'a changé."
NAME_TAKEN = "Un format porte déjà ce nom."
#: Said beside a « Ventes » format that reads no amount.
NO_MONEY = "sans colonne de montant : la recette de ces jours reste « non lue »"
FORMAT_ANCHOR = "format-{pk}"


@dataclass(frozen=True)
class FormatExample:
    """« Lire un format »'s worked example: an invented export, the format
    that reads it, and what it reads (recipes/tests/test_till_format_views.py
    reads it so)."""

    rows: tuple
    settings: dict
    said: str
    reads: str


FORMAT_EXAMPLE = FormatExample(
    rows=(
        "Date;Article;Qté;Total TTC;TVA",
        "03/07/2026 23:41;Pinte Exemple;2;13,00;20 %",
        "04/07/2026 01:30;Pinte Exemple;1;6,50;20 %",
    ),
    settings={
        "kind": "ventes",
        "encoding": "auto",
        "delimiter": ";",
        "decimal_mark": ",",
        "date_format": "dd/mm/yyyy",
        "service_day_end_hour": 5,
        "day_column": "Date",
        "product_column": "Article",
        "quantity_column": "Qté",
        "amount_column": "Total TTC",
        "rate_column": "TVA",
    },
    said=(
        "Contenu « Ventes par produit », séparateur « ; », dates jj/mm/aaaa, décimales à la virgule, fin du service 5 ; "
        "colonnes « Date », « Article », « Qté », « Total TTC », « TVA »"
    ),
    reads="3 « Pinte Exemple » le 03/07/2026 (la vente de 01:30 compte pour la veille), 19,50 € TTC, 16,25 € HT",
)


@dataclass
class FormatRow:
    """One stored format, as the list draws it."""

    fmt: TillFormat
    #: Which column holds what: « jour Date · produit Article · quantité 3 ».
    columns: str
    #: Why the format no longer passes the check, in French - « » when it does.
    problem: str = ""
    #: A « Ventes » format reading no amount.
    no_money: bool = False


def columns_said(fmt) -> str:
    return " · ".join(
        f"{till_file.ROLE_WORDS[role]} {value}"
        for role in till_file.COLUMN_FIELDS
        if (value := str(getattr(fmt, role, "") or "").strip())
    )


def format_problem(fmt: TillFormat) -> str:
    """Why a stored format no longer passes `check_format` - « Colonne du
    produit : indiquez … » -, or « ». A stored format is never trusted: an
    upload refuses to read with one the check refuses."""
    try:
        till_file.check_format(fmt)
    except till_file.FormatError as error:
        said = error.message.rstrip(".")
        try:
            label = str(capfirst(TillFormat._meta.get_field(error.field).verbose_name))
        except FieldDoesNotExist:
            return said
        return f"{label} : {said[:1].lower()}{said[1:]}"
    return ""


def format_rows() -> list[FormatRow]:
    return [
        FormatRow(
            fmt,
            columns_said(fmt),
            format_problem(fmt),
            no_money=fmt.kind == TillFormat.Kind.SALES and not fmt.amount_column.strip(),
        )
        for fmt in TillFormat.objects.order_by("name")
    ]


def format_url(pk) -> str:
    return f"{reverse('recipes:till_formats')}#{FORMAT_ANCHOR.format(pk=pk)}"


@dataclass
class FormatTest:
    """What « Tester » read in a file with the format as typed."""

    file_name: str = ""
    #: Why the file was not read at all: none chosen, too heavy, no .csv.
    problem: str = ""
    #: The first rows, (number, cells), each as wide as the widest.
    rows: list = field(default_factory=list)
    width: int = 0
    wider: bool = False
    column_limit: int = till_file.MAX_COLUMN
    #: Why the format reads nothing in the file - the import's own sentence.
    refusal: str = ""
    reading: till_file.TillReading | None = None
    #: [(role's word, column number, header's text)].
    mapping: list = field(default_factory=list)
    kind: str = TillFormat.Kind.SALES
    #: Sales: the product names already known to the till, and the new ones
    #: (« à lier ») - the first ones.
    known_products: int = 0
    new_products: list = field(default_factory=list)
    new_count: int = 0
    #: Payments: [(label, amount, count)] per method.
    methods: list = field(default_factory=list)
    #: The day read for a format with no day column.
    day: object = None

    @property
    def columns(self) -> range:
        return range(1, self.width + 1)

    @property
    def export(self):
        return self.reading.export if self.reading else None

    @property
    def new_more(self) -> int:
        return self.new_count - len(self.new_products)


def _known_names(names: list[str]) -> set[str]:
    """The till names already on file - in chunks: SQLite caps one query's
    parameters."""
    known: set[str] = set()
    for start in range(0, len(names), 500):
        known |= set(PosProduct.objects.filter(name__in=names[start : start + 500]).values_list("name", flat=True))
    return known


def _test_day(request):
    """The day « Tester » reads a format with no day column at: the one
    posted, else today."""
    return read_date(request.POST.get(TEST_DAY)) or timezone.localdate()


def test_format(request, form: TillFormatForm) -> FormatTest | None:
    """The file posted as `TEST_FILE` read with the format as typed - nothing
    saved, the file never kept. None while the format is refused (its errors
    are on the form)."""
    layout = form.layout
    if layout is None:
        return None
    upload = request.FILES.get(TEST_FILE)
    if upload is None:
        return FormatTest(problem=NO_TEST_FILE)
    test = FormatTest(upload.name, kind=layout.kind)
    try:
        till_file.check_suffix(upload.name)
    except till_file.TillFileError as refusal:
        test.problem = f"{upload.name} : {refusal}"
        return test
    too_heavy = file_too_big(upload)
    if too_heavy:
        test.problem = too_heavy
        return test
    content = upload.read()
    try:
        shown = till_file.preview(io.BytesIO(content), layout, upload.name, TEST_ROWS_SHOWN)
    except till_file.TillFileError as refusal:
        test.refusal = str(refusal)
        return test
    widest = max((len(cells) for _number, cells in shown), default=0)
    test.width = min(widest, till_file.MAX_COLUMN)
    test.wider = widest > till_file.MAX_COLUMN
    test.rows = [(number, (cells + [""] * test.width)[: test.width]) for number, cells in shown]
    test.day = None if layout.has(till_file.DAY) else _test_day(request)
    try:
        test.reading = till_file.read(
            io.BytesIO(content), layout, file_name=upload.name, day=test.day, limit=TEST_ROW_LIMIT
        )
    except till_file.TillFileError as refusal:
        test.refusal = str(refusal)
        return test
    test.mapping = [
        (till_file.ROLE_WORDS[role], number, title)
        for role in till_file.COLUMN_FIELDS
        if role in test.reading.mapped
        for number, title in [test.reading.mapped[role]]
    ]
    export = test.reading.export
    if layout.kind == TillFormat.Kind.SALES:
        names = sorted(export.products)
        known = _known_names(names)
        new = [name for name in names if name not in known]
        test.known_products = len(known)
        test.new_count = len(new)
        test.new_products = new[:TEST_PRODUCTS_SHOWN]
    else:
        test.methods = [
            (PosDailyPayment.label_for(method), payment.amount, payment.count)
            for method, payment in by_method(export.payments_by_method())
        ]
    return test


def _saved(form: TillFormatForm) -> TillFormat | None:
    """The format saved, or None with the name refused: checked by a read
    before the write, a name another tab saved meanwhile is the unique
    constraint's - said on the name, never a 500."""
    try:
        with transaction.atomic():
            return form.save()
    except IntegrityError:
        form.add_error("name", NAME_TAKEN)
        return None


def _page(request, template: str, context: dict):
    return render(
        request, template, {"example": FORMAT_EXAMPLE, "test_file": TEST_FILE, "test_day": TEST_DAY, **context}
    )


def till_formats(request):
    """« Formats des fichiers de caisse »: the formats, and a new one -
    « Tester » reads a file with it and saves nothing; « Enregistrer le
    format » keeps it."""
    form = TillFormatForm(request.POST or None)
    test = None
    if request.method == "POST":
        action = request.POST.get(ACTION)
        if action not in (TEST, SAVE):
            messages.error(request, UNKNOWN_ACTION)
            return redirect("recipes:till_formats")
        valid = form.is_valid()
        if action == SAVE and valid:
            fmt = _saved(form)
            if fmt is not None:
                messages.success(request, f"Format « {fmt.name} » ajouté.")
                return redirect(format_url(fmt.pk))
        if action == TEST:
            test = test_format(request, form)
    return _page(request, "recipes/till_formats.html", {"form": form, "test": test, "rows": format_rows()})


def till_format(request, pk):
    """One format: its form on a GET (which writes nothing), « Tester »,
    « Enregistrer », « Supprimer »."""
    fmt = get_object_or_404(TillFormat, pk=pk)
    action = request.POST.get(ACTION) if request.method == "POST" else None
    if request.method == "POST" and action == DELETE:
        name = fmt.name
        fmt.delete()
        messages.success(request, f"Format « {name} » supprimé.")
        return redirect("recipes:till_formats")
    if request.method == "POST" and action not in (TEST, SAVE):
        messages.error(request, UNKNOWN_ACTION)
        return redirect("recipes:till_format", pk=pk)
    test = None
    if request.method != "POST":
        form = TillFormatForm(instance=fmt)
    else:
        # Bound to a copy: validating writes what was typed onto the
        # instance, and the page's title would name a refused name.
        form = TillFormatForm(request.POST, instance=TillFormat.objects.get(pk=fmt.pk))
        valid = form.is_valid()
        if action == SAVE and valid:
            saved = _saved(form)
            if saved is not None:
                messages.success(request, f"Format « {saved.name} » enregistré.")
                return redirect(format_url(saved.pk))
        if action == TEST:
            test = test_format(request, form)
    return _page(
        request,
        "recipes/till_format.html",
        {"fmt": fmt, "form": form, "test": test, "problem": format_problem(fmt)},
    )


# -- the upload on « Ventes » ----------------------------------------------------------

#: The upload's fields - the page's HTTP interface.
UPLOAD_FILE = "fichier"
UPLOAD_FORMAT = "format"
UPLOAD_DAY = "jour"
CHOOSE_A_FILE = "Choisissez un fichier."
ALREADY_RUNNING = "Une récupération est déjà en cours."
DAY_UNREAD = "Jour des ventes illisible."
DAY_NEEDED = "Indiquez le jour des ventes : ce format n'a pas de colonne du jour."
DAY_TO_COME = "Le jour des ventes est à venir : vérifiez-le."


def _uploader(request) -> str:
    user = request.user
    return (user.get_full_name() or user.first_name or user.get_username() or "").strip()


def upload_sales_file(request):
    """« Importer un fichier de la caisse » (POST): the file checked - its
    weight, its kind, its choice (L'Addition's export or a format, which
    must pass the check) and its day - then staged under the espace's
    imports/ and read by a job, like the fetch: the same status card, cancel
    and « Données » busy check. Nothing is kept here: the job moves a file
    read whole into place, and deletes a refused one."""
    from .menu import sales_list_url
    from .models import SalesImportJob
    from .pos.connectors import resolve
    from .tasks import import_till_file_task, staged_uploads_dir

    back = redirect(sales_list_url(request))
    if request.method != "POST":
        return back
    upload = request.FILES.get(UPLOAD_FILE)
    if upload is None:
        messages.error(request, CHOOSE_A_FILE)
        return back
    too_heavy = file_too_big(upload)
    if too_heavy:
        messages.error(request, too_heavy)
        return back
    try:
        choice = resolve(request.POST.get(UPLOAD_FORMAT, ""), upload.name)
    except till_file.TillFileError as refusal:
        messages.error(request, str(refusal))
        return back
    posted_day = (request.POST.get(UPLOAD_DAY) or "").strip()
    day = read_date(posted_day) if posted_day else None
    if posted_day and day is None:
        messages.error(request, DAY_UNREAD)
        return back
    if day is not None and day > timezone.localdate():
        messages.error(request, DAY_TO_COME)
        return back
    reads_a_day = choice.laddition or choice.layout.has(till_file.DAY)
    if day is not None and reads_a_day:
        messages.error(request, till_file.DAY_GIVEN_TWICE)
        return back
    if day is None and not reads_a_day:
        messages.error(request, DAY_NEEDED)
        return back
    # A dead run is reaped first, or one killed thread locks the page out.
    SalesImportJob.reap_stale()
    if SalesImportJob.objects.filter(
        status__in=[SalesImportJob.Status.PENDING, SalesImportJob.Status.RUNNING]
    ).exists():
        messages.error(request, ALREADY_RUNNING)
        return back
    staged = staged_uploads_dir() / f"{uuid.uuid4().hex}.part"
    with open(staged, "wb") as handle:
        handle.writelines(upload.chunks())
    job = SalesImportJob.objects.create()
    # bound(): the thread works for this request's tenant - its job row, its
    # sales, its folders - and closes its connections when it ends.
    threading.Thread(
        target=bound(import_till_file_task),
        args=(job.id, str(staged), request.POST.get(UPLOAD_FORMAT, ""), upload.name, day, _uploader(request)),
        daemon=True,
    ).start()
    return back
