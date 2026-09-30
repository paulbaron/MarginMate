"""« Personnel »: the employees, their typical week, and each month's
timesheet (« fiche de temps »).

Three pages, and every figure on them comes from `staff.timesheet`: a view
reads the request, calls it, and says in French what happened.

* `/personnel/` (`home`): the establishment's header (what every sheet
  prints at its top), the employees, and « Ajouter un salarié ».
* `/personnel/<pk>/` (`employee`): the name and the typical week, active or
  not, and the months - every saved one, plus this month while it is not.
* `/personnel/<pk>/<yyyy>-<mm>/` (`month`): the month's grid - one form, one
  « Enregistrer » - and beside it the three shortcuts (« Du … au … », the
  holidays, back to the typical week), each a POST answering with a redirect
  to the month and a message saying what changed.

And `/personnel/<pk>/<yyyy>-<mm>/pdf/` (`month_pdf`): the month as the PDF
to print and have signed (`staff.pdf`).

The month's page also carries « Signature », the monthly electronic
signature (staff/signature_views.py, `render_month`): while a request holds
the month it is drawn READ-ONLY - its days as a table, no grid, no shortcut
- and `timesheet._store` refuses a write whatever page posts it
(`MonthLocked`, said by each action here). The employee's own pages, the
only ones meant to be reachable without an account, are
staff/public_views.py.

Three rules shape them:

**A page without JavaScript works.** Every action is a plain form posting
to its own address; `static/js/timesheet.js` only empties the hours of an
absence as it is chosen (the server enforces it whatever it is sent,
`timesheet._settle`), makes the month's figures follow the typing until the
grid is saved, asks before a grid changed and not saved is left
(`data-leaves-grid`: the three shortcuts reload the month from the
database, and the PDF prints it as saved), and adds up the typical week as
it is typed.

**The grid is not a formset.** Its fields are named by the day's ISO date
(`heures-2026-06-04`), so no gap in a row index can shift a day's hours onto
its neighbour - the bug CLAUDE.md « Formsets: test what the browser
actually posts » records three times. A refused save draws the page back
with what was typed and each day's error beside it, and writes nothing.

**A bad address is a 404, never a 500**: the month is a path converter
(`urls.MonthConverter`), so « 2026-13 » never reaches a view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from common import is_id

from . import signature_views, timesheet
from .forms import EMAIL_FIELD, NAME_FIELDS, EmployeeForm, EstablishmentForm
from .models import ABSENCE_KINDS, WEEKDAY_FIELDS, Employee, Establishment, Timesheet
from .pdf import content_disposition, render_month_pdf
from .signature_requests import month_snapshot
from .timesheet import (
    FIRST_YEAR,
    KIND_CHOICES,
    KIND_LABELS,
    LAST_YEAR,
    MONTH_NAMES,
    WORK,
    HoursError,
    MonthSheet,
    day_name,
    first_of_month,
    format_hours,
    month_label,
    month_name,
    month_sheet,
    month_title,
    parse_month,
    parse_optional_hours,
    saved_month_sheets,
    span_label,
    week_summary,
)

#: What the two forms of the list page post to say which one they are.
ACTION_FIELD = "action"
SAVE_ESTABLISHMENT = "etablissement"
ADD_EMPLOYEE = "ajouter"

#: « Du … au … »: the fields of its form. Not `du`/`au`, which mean the
#: GET period of six pages (CLAUDE.md « Du … au … »), and a test looking for
#: that form by its field names would find the wrong one.
RANGE_START = "debut"
RANGE_END = "fin"
RANGE_KIND = "motif"
RANGE_HOURS = "heures"
RANGE_NOTE = "note"
#: What « Du … au … » offers first: leave is what it is used for most.
RANGE_DEFAULT_KIND = timesheet.Kind.PAID_LEAVE.value

#: « Ouvrir un autre mois »: the month (1-12, or « 2026-06 » as an
#: `<input type="month">` would send it) and the year.
PICK_MONTH = "mois"
PICK_YEAR = "annee"

#: How many days a message names before it says « et N autres ».
NAMED_AT_MOST = 6

#: The month's page, before its grid's unsaved changes are lost.
LEAVE_WARNING = "Des modifications de la grille ne sont pas enregistrées : elles seront perdues. Continuer quand même ?"
PDF_WARNING = (
    "Des modifications de la grille ne sont pas enregistrées : le PDF imprime la fiche telle qu'elle est "
    "enregistrée, sans elles. Télécharger quand même ?"
)

_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def this_month() -> date:
    """The month the pages open on: today's in the app's time zone (Paris),
    not UTC's - at 00:30 on the 1st, UTC is still in last month."""
    return first_of_month(timezone.localdate())


def _employee(pk: int) -> Employee:
    # `<int:pk>` is any run of digits. Past SQLite's integer range, Django 5.2
    # answers this lookup with « no row » rather than raising (checked
    # 27/09), so a 23-digit address is a 404; NotFoundTests pins it.
    return get_object_or_404(Employee, pk=pk)


def _month_url(person: Employee, month: date) -> str:
    return reverse("staff:month", args=[person.pk, month])


def _of(month: date) -> str:
    """« de juin 2026 », « d'août 2026 » - with the elision `month_title`
    makes, so « la fiche de août » cannot be written."""
    return month_title(month).removeprefix("Mois ")


def _in_prose(day: date) -> str:
    """« 1er mai », « 14 juillet »."""
    return f"{'1er' if day.day == 1 else day.day} {month_name(day)}"


def _days_named(days) -> str:
    """« mardi 2, mercredi 3 et jeudi 4 » - and past NAMED_AT_MOST, « … et
    12 autres »."""
    names = [day_name(day).lower() for day in days]
    if len(names) > NAMED_AT_MOST:
        return ", ".join(names[:NAMED_AT_MOST]) + f" et {len(names) - NAMED_AT_MOST} autres"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " et " + names[-1]


def _days_off_left(names: list[str]) -> str:
    """« Jours de repos laissés tels quels : dimanche 14 et lundi 15. » -
    what a range or the holidays button did not touch, and why."""
    if len(names) == 1:
        return f"Jour de repos laissé tel quel : {names[0]}."
    listing = ", ".join(names[:-1]) + " et " + names[-1]
    return f"Jours de repos laissés tels quels : {listing}."


def _changed_words(days, *, name_them: bool = True, against: str = "") -> str:
    """« 1 jour modifié (mardi 2) », « 3 jours modifiés par rapport à la
    semaine type (…) », « aucun jour modifié »."""
    if not days:
        return "aucun jour modifié"
    count = "1 jour modifié" if len(days) == 1 else f"{len(days)} jours modifiés"
    if against:
        count = f"{count} {against}"
    return f"{count} ({_days_named(days)})" if name_them else count


# -- /personnel/ ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmployeeRow:
    employee: Employee
    week: str  # « Ma 7,5 · Me 6 · Je–Sa 7,5 · 36 h / semaine »
    this_month_saved: bool  # whether this month's sheet is saved yet


def home(request):
    """The establishment's header, the employees, « Ajouter un salarié ».
    Both forms post here, each saying which it is (`action`): a refused one
    is drawn back with its errors, the other one untouched."""
    establishment = Establishment.current()
    establishment_form = EstablishmentForm(instance=establishment)
    employee_form = EmployeeForm()
    if request.method == "POST":
        action = request.POST.get(ACTION_FIELD)
        if action == SAVE_ESTABLISHMENT:
            establishment_form = EstablishmentForm(request.POST, instance=Establishment.current())
            if establishment_form.is_valid():
                _say_header(request, establishment_form.save())
                return redirect("staff:home")
        elif action == ADD_EMPLOYEE:
            employee_form = EmployeeForm(request.POST)
            if employee_form.is_valid():
                person = employee_form.save()
                messages.success(request, f"Salarié ajouté : {person.display_name}, {week_summary(person)}.")
                return redirect("staff:home")
        else:
            messages.error(request, "Action inconnue : rien n'a été modifié.")
            return redirect("staff:home")

    month = this_month()
    saved_now = set(Timesheet.objects.filter(month=month).values_list("employee_id", flat=True))
    # Active first: an employee who left stays listed (their sheets are
    # kept) but under the ones whose month is being filled in.
    rows = [
        EmployeeRow(person, week_summary(person), person.pk in saved_now)
        for person in Employee.objects.order_by("-is_active", "last_name", "first_name")
    ]
    return render(
        request,
        "staff/home.html",
        {
            "establishment": establishment,
            "establishment_form": establishment_form,
            "employee_form": employee_form,
            "rows": rows,
            "this_month": month,
            "this_month_label": month_label(month),
            "action_field": ACTION_FIELD,
            "save_establishment": SAVE_ESTABLISHMENT,
            "add_employee": ADD_EMPLOYEE,
        },
    )


def _say_header(request, establishment: Establishment) -> None:
    printed = [establishment.name.strip(), *establishment.address_lines] if establishment.name.strip() else []
    if printed:
        messages.success(request, f"En-tête des fiches enregistré : {', '.join(printed)}.")
    elif establishment.address_lines:
        # A sheet with no name prints no header at all (`pdf._establishment_lines`):
        # an address under nothing says nothing.
        messages.warning(
            request,
            "Adresse enregistrée, mais sans nom d'établissement les fiches s'impriment sans en-tête.",
        )
    else:
        messages.success(request, "En-tête des fiches effacé : elles s'impriment sans nom ni adresse.")


# -- /personnel/<pk>/ -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthRow:
    """A line of the employee's months."""

    sheet: MonthSheet
    url: str

    @property
    def absences(self) -> str:
        """« Congés payés 5 jours · Férié chômé 1 jour », "" when none."""
        return " · ".join(f"{count.label} {count.days_text}" for count in self.sheet.summary.absences)

    @property
    def saved_at(self) -> datetime | None:
        return self.sheet.timesheet.updated_at if self.sheet.saved else None


def employee(request, pk):
    """The name and the typical week, and the months. Changing the week
    never rewrites a month already saved - that is what the employee signed
    - and the page and its message both say so."""
    person = _employee(pk)
    if request.method == "POST":
        # Bound to a copy of its own: a refused form writes what was typed
        # onto its instance, and the page's header and its list of months
        # must go on showing the employee as saved.
        form = EmployeeForm(request.POST, instance=_employee(pk))
        if form.is_valid():
            _say_employee_saved(request, form)
            return redirect("staff:employee", pk=person.pk)
    else:
        form = EmployeeForm(instance=person)

    current = this_month()
    sheets = saved_month_sheets(person)
    if not any(sheet.month == current for sheet in sheets):
        sheets.append(month_sheet(person, current))
        sheets.sort(key=lambda sheet: sheet.month, reverse=True)
    rows = [MonthRow(sheet, _month_url(person, sheet.month)) for sheet in sheets]
    return render(
        request,
        "staff/employee.html",
        {
            "employee": person,
            "week": week_summary(person),
            "form": form,
            "rows": rows,
            "saved_count": sum(1 for row in rows if row.sheet.saved),
            "this_month": current,
            "this_month_label": month_label(current),
            "month_choices": list(enumerate(MONTH_NAMES, start=1)),
            "pick_month": PICK_MONTH,
            "pick_year": PICK_YEAR,
            "first_year": FIRST_YEAR,
            "last_year": LAST_YEAR,
        },
    )


def _say_employee_saved(request, form: EmployeeForm) -> None:
    changed = set(form.changed_data)
    person = form.save()
    if not changed:
        messages.info(request, "Rien n'a changé.")
        return
    if changed & set(NAME_FIELDS):
        messages.success(request, f"Nom enregistré : {person.display_name}.")
    if EMAIL_FIELD in changed:
        if person.email:
            messages.success(request, f"E-mail enregistré : {person.email}.")
        else:
            messages.success(request, "E-mail effacé : le lien de signature et le code se transmettront sans e-mail.")
    if changed & set(WEEKDAY_FIELDS):
        saved = person.timesheets.count()
        if saved == 0:
            after = "Les mois suivent la nouvelle semaine type."
        elif saved == 1:
            after = (
                "Le mois déjà enregistré garde ses heures et sa semaine type ; les autres suivent la nouvelle "
                "semaine type."
            )
        else:
            after = (
                f"Les {saved} mois déjà enregistrés gardent leurs heures et leur semaine type ; les autres suivent "
                "la nouvelle semaine type."
            )
        messages.success(request, f"Semaine type enregistrée : {week_summary(person)}. {after}")


def employee_active(request, pk):
    """Deactivate or reactivate - never delete: a signed timesheet is a
    record the employer keeps (`Timesheet.employee` is PROTECT). The form
    says which state it wants, so a second click is not a second toggle."""
    person = _employee(pk)
    if request.method != "POST":
        return redirect("staff:employee", pk=person.pk)
    wanted = request.POST.get("actif")
    if wanted not in ("0", "1"):
        messages.error(request, "Action inconnue : rien n'a été modifié.")
        return redirect("staff:employee", pk=person.pk)
    active = wanted == "1"
    if person.is_active == active:
        messages.info(request, f"{person.display_name} : rien n'a changé.")
    else:
        person.is_active = active
        person.save(update_fields=["is_active"])
        if active:
            messages.success(request, f"{person.display_name} est de nouveau parmi les salariés actifs.")
        else:
            messages.success(
                request,
                f"{person.display_name} n'est plus parmi les salariés actifs. Ses fiches de temps sont conservées "
                "et restent consultables ici.",
            )
    return redirect("staff:employee", pk=person.pk)


def open_month(request, pk):
    """« Ouvrir un autre mois »: the month and year chosen, to the month's
    own address - or back to the employee, saying what was wrong."""
    person = _employee(pk)
    month = _picked_month(request.GET.get(PICK_MONTH, ""), request.GET.get(PICK_YEAR, ""))
    if month is None:
        messages.error(request, f"Choisissez un mois et une année, de {FIRST_YEAR} à {LAST_YEAR}.")
        return redirect("staff:employee", pk=person.pk)
    return redirect("staff:month", pk=person.pk, month=month)


def _picked_month(month_text: str, year_text: str) -> date | None:
    month_text, year_text = month_text.strip(), year_text.strip()
    whole = parse_month(month_text)
    if whole is not None:
        return whole
    if not (is_id(month_text) and is_id(year_text)) or len(month_text) > 2 or len(year_text) > 4:
        return None
    return parse_month(f"{int(year_text):04d}-{int(month_text):02d}")


# -- /personnel/<pk>/<yyyy>-<mm>/ -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Neighbour:
    """« ← mai », « juillet → »."""

    name: str
    url: str


def _neighbour(person: Employee, month: date) -> Neighbour | None:
    # None past the years an address may name: 1900-01 has no « ← ».
    if not FIRST_YEAR <= month.year <= LAST_YEAR:
        return None
    return Neighbour(month_name(month), _month_url(person, month))


def month(request, pk, month):
    """The month's grid. GET draws it (the saved days, or the typical week
    with a banner saying so); POST saves every day the form sent, or draws
    the page back with what was typed when a day is refused."""
    person = _employee(pk)
    if request.method == "POST":
        posted = timesheet.read_posted_month(month, request.POST)
        if not posted.days:
            messages.error(request, "Aucun jour n'a été envoyé : rien n'a été enregistré.")
            return redirect(_month_url(person, month))
        if posted.is_valid:
            try:
                outcome = timesheet.save_month(person, month, posted.days)
            except ValueError as error:
                messages.error(request, f"Rien n'a été enregistré : {error}")
                return redirect(_month_url(person, month))
            _say_saved(request, month, outcome)
            return redirect(_month_url(person, month))
        sheet = month_sheet(person, month, posted=posted)
    else:
        sheet = month_sheet(person, month)
    return render_month(request, person, sheet)


def render_month(request, person: Employee, sheet: MonthSheet, *, signature=None):
    """The month's page. `signature` is its « Signature » section when the
    answer draws something once (a link, a code, a report -
    staff/signature_views.py); by default the section as it stands. A month
    a request holds is drawn read-only: its days as a table, no grid, no
    shortcut."""
    if signature is None:
        signature = signature_views.panel(person, sheet)
    context = _month_context(person, sheet)
    context["signature"] = signature
    if signature.locked:
        # The days as the employee's page shows them, from the same builder.
        context["month_table"] = month_snapshot(sheet, None)
    return render(request, "staff/month.html", context)


def _month_context(person: Employee, sheet: MonthSheet) -> dict:
    month = sheet.month
    return {
        "employee": person,
        "sheet": sheet,
        "summary": sheet.summary,
        "kind_choices": KIND_CHOICES,
        # The kinds whose hours timesheet.js empties and disables.
        "absence_kinds": " ".join(sorted(ABSENCE_KINDS)),
        "previous": _neighbour(person, sheet.previous_month),
        "next": _neighbour(person, sheet.next_month),
        "employee_url": reverse("staff:employee", args=[person.pk]),
        "pdf_url": reverse("staff:month_pdf", args=[person.pk, month]),
        "save_url": _month_url(person, month),
        "range_url": reverse("staff:month_range", args=[person.pk, month]),
        "holidays_url": reverse("staff:month_holidays_off", args=[person.pk, month]),
        "reset_url": reverse("staff:month_reset", args=[person.pk, month]),
        "holidays": [(day, f"{_in_prose(day.date)} — {day.holiday}") for day in sheet.holidays],
        "range_start": RANGE_START,
        "range_end": RANGE_END,
        "range_kind": RANGE_KIND,
        "range_hours": RANGE_HOURS,
        "range_note": RANGE_NOTE,
        "range_default_kind": RANGE_DEFAULT_KIND,
        "of_month": _of(month),
        # What timesheet.js asks before leaving a grid changed and not saved
        # (`data-leaves-grid`): the three forms beside it reload the month
        # from the database, and the PDF prints the month as SAVED - a sheet
        # handed over to be signed without the corrections just typed.
        "leave_warning": LEAVE_WARNING,
        "pdf_warning": PDF_WARNING,
    }


def _say_saved(request, month: date, outcome: timesheet.Outcome) -> None:
    # A first save is said against the typical week, which is what the page
    # showed; a later one against what was saved before.
    if outcome.created and not outcome.changed:
        detail = "c'est la semaine type, sans changement"
    elif outcome.created:
        detail = _changed_words(outcome.changed, against="par rapport à la semaine type")
    else:
        detail = _changed_words(outcome.changed)
    messages.success(request, f"Fiche {_of(month)} enregistrée : {detail}.")
    for adjustment in outcome.adjustments:
        messages.info(request, adjustment)


def _posted_day(text) -> date | None:
    """A day as the « Du … au … » form sends it (« 2026-06-04 »). Only that
    shape: `date.fromisoformat` also reads « 20260604 » and « 2026-W23-4 »,
    which no form of this page sends."""
    text = (text or "").strip()
    if not _ISO_DAY.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def month_range(request, pk, month):
    """« Du … au … : [motif] [heures facultatives] » - « il était en congés
    du 11 au 18 » in one action (`timesheet.apply_range`). The days off it
    leaves as they are are named: « 6 jours modifiés » under a range of
    eight has to say why."""
    person = _employee(pk)
    back = redirect(_month_url(person, month))
    if request.method != "POST":
        return back
    start = _posted_day(request.POST.get(RANGE_START))
    end = _posted_day(request.POST.get(RANGE_END))
    kind = (request.POST.get(RANGE_KIND) or "").strip()
    if start is None or end is None:
        messages.error(request, "« Du … au … » : choisissez le premier et le dernier jour. Rien n'a été modifié.")
        return back
    if kind not in KIND_LABELS:
        messages.error(request, "« Du … au … » : motif inconnu. Rien n'a été modifié.")
        return back
    try:
        hours = parse_optional_hours(request.POST.get(RANGE_HOURS), "« Du … au … »")
    except HoursError as error:
        messages.error(request, f"{error} Rien n'a été modifié.")
        return back
    # Blank is « not said »: each day keeps its note if its kind stays.
    note = " ".join((request.POST.get(RANGE_NOTE) or "").split()) or None
    try:
        outcome = timesheet.apply_range(person, month, start, end, kind, hours, note)
    except ValueError as error:
        # Its sentences name the dates or the note: « Du 04/07/2026 au
        # 05/07/2026 : aucun jour en juin 2026. »
        messages.error(request, f"{error} Rien n'a été modifié.")
        return back

    first, last = min(start, end), max(start, end)
    span = span_label(outcome.touched[0], outcome.touched[-1])
    if kind == WORK and hours is None:
        what = f"Travail aux heures de la semaine type {span}"
    elif kind == WORK:
        what = f"Travail, {format_hours(hours)} h par jour, {span}"
    else:
        what = f"{KIND_LABELS[kind]} {span}"
    text = f"{what} : {_changed_words(outcome.changed)}."
    if outcome.left_alone:
        text += " " + _days_off_left([day_name(day).lower() for day in outcome.left_alone])
    if outcome.created:
        text += f" La fiche {_of(month)} est maintenant enregistrée."
    (messages.success if outcome.changed else messages.info)(request, text)
    if first < outcome.touched[0] or last > outcome.touched[-1]:
        messages.warning(
            request,
            f"La période demandée, {span_label(first, last)}, dépasse {month_label(month)} : seuls ses jours "
            "ont été modifiés.",
        )
    if hours is not None and kind != WORK:
        messages.warning(
            request,
            f"Les {format_hours(hours)} h saisies n'ont pas été comptées : un jour de « {KIND_LABELS[kind]} » "
            "compte 0 h. Une demi-journée, c'est « Travail » avec ses heures et une note.",
        )
    return back


def month_holidays_off(request, pk, month):
    """« Mettre les fériés du mois en Férié chômé » - the owner's decision,
    taken with this button, never assumed (`timesheet.mark_holidays_off`).
    A holiday on a day off stays a day off, and the answer names it."""
    person = _employee(pk)
    back = redirect(_month_url(person, month))
    if request.method != "POST":
        return back
    holidays = timesheet.month_holidays(month)
    if not holidays:
        messages.info(request, f"Aucun jour férié en {month_label(month)} : rien n'a été modifié.")
        return back
    try:
        outcome = timesheet.mark_holidays_off(person, month)
    except timesheet.MonthLocked as error:
        # A page drawn before the month was sent for signature.
        messages.error(request, f"{error} Rien n'a été modifié.")
        return back
    named = {day: f"{_in_prose(day)} ({name})" for day, name in sorted(holidays.items())}
    left = _days_off_left([named[day] for day in outcome.left_alone]) if outcome.left_alone else ""
    marked = [label for day, label in named.items() if day not in outcome.left_alone]
    if not marked:
        messages.info(request, f"{left} Rien n'a été modifié.")
        return back
    listing = marked[0] if len(marked) == 1 else ", ".join(marked[:-1]) + " et " + marked[-1]
    if outcome.changed:
        text = f"En Férié chômé, 0 h : {listing} — {_changed_words(outcome.changed, name_them=False)}."
    else:
        text = f"Déjà en Férié chômé : {listing}."
    if left:
        text += f" {left}"
    if not outcome.changed:
        text += " Rien n'a changé."
    if outcome.created:
        text += f" La fiche {_of(month)} est maintenant enregistrée."
    messages.success(request, text)
    return back


def month_reset(request, pk, month):
    """« Revenir à la semaine type »: the saved month rewritten from the
    employee's typical week of today, notes cleared, and that week becomes
    the month's own (`timesheet.reset_to_typical_week`) - said when it was
    another one, since the sheet's « Semaine type » changes with it. The
    page asks first."""
    person = _employee(pk)
    back = redirect(_month_url(person, month))
    if request.method != "POST":
        return back
    try:
        outcome = timesheet.reset_to_typical_week(person, month)
    except timesheet.MonthLocked as error:
        messages.error(request, f"{error} Rien n'a été modifié.")
        return back
    of = _of(month)
    if outcome.timesheet is None:
        messages.info(request, f"La fiche {of} n'est pas encore enregistrée : elle suit déjà la semaine type.")
    elif outcome.week_changed:
        messages.success(
            request,
            f"Fiche {of} remise à la semaine type d'aujourd'hui ({format_hours(person.weekly_hours)} h au lieu de "
            f"{format_hours(outcome.week_before)} h), notes effacées : {_changed_words(outcome.changed)}.",
        )
    elif outcome.changed:
        messages.success(
            request, f"Fiche {of} remise à la semaine type, notes effacées : {_changed_words(outcome.changed)}."
        )
    else:
        messages.info(request, f"La fiche {of} était déjà la semaine type : rien n'a changé.")
    return back


def month_pdf(request, pk, month):
    """The month's sheet as a PDF to download (`staff.pdf`): « Fiche de temps
    DUPONT Jeanne juin 2026.pdf », the one to print and have signed.

    Saved or not: a month nobody saved prints its typical week, which is
    exactly what « Enregistrer » would store, so the PDF of an untouched
    month is the PDF of that month once saved. Nothing is written - not the
    month, and not the header's row either: `Establishment.current()` creates
    it, and a download is a GET. With no row, the sheet prints no header,
    which is what a blank one prints too."""
    person = _employee(pk)
    sheet = month_sheet(person, month)
    establishment = Establishment.objects.filter(pk=Establishment.SINGLETON_PK).first()
    response = HttpResponse(render_month_pdf(sheet, establishment), content_type="application/pdf")
    # Plain ASCII whatever the name: an ASCII fallback, then the exact name
    # (accents, « août ») in RFC 5987's filename*.
    response["Content-Disposition"] = content_disposition(sheet)
    return response
