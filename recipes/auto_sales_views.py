"""« Import automatique des ventes » (/recipes/import-auto/): the rules of
recipes/auto_sales.py, one card and one form per rule, each posting to its
own address with its card's fragment (the automatic gathers' page, the
precedent).

The espace owner's alone, and only where a sales source may be used
(sales_sources: L'Addition's `available()`, the owner's espace) - anywhere
else the page says why and draws no form, and every POST gets the 403 page.
A member reads the rules without a form. A GET on a POST-only route goes
back to the page and writes nothing; saving never runs an import at once
(creating, re-activating or a new schedule sets `last_slot_at = now`
through its own update) and never writes the scheduler's columns.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.tenancy import is_owner
from notifications import schedule, webpush

from . import auto_sales, sales_sources
from .forms import AutoSalesImportForm
from .models import AutoSalesImport, SalesImportJob

#: Said to a member, who sees the rules and not their forms.
OWNER_ONLY_SETTINGS = "Seul le propriétaire de l'espace modifie ces réglages."
AUTO_SALES_CAP = f"{AutoSalesImport.MAX_PER_TENANT} imports automatiques au plus : supprimez-en un."
#: The latest runs listed under each rule.
AUTO_SALES_RUNS_SHOWN = 10
#: The new rule's form prefix (#nouvel-import).
NEW_AUTO_SALES = "nouveau"
#: The fields drawn by _form_fields.html above and below the hand-drawn days.
HEAD_FIELDS = ["name", "source"]
TAIL_FIELDS = ["times", "is_active"]


def _page_url(pk=None) -> str:
    url = reverse("recipes:auto_sales")
    return f"{url}#import-{pk}" if pk else url


def _available() -> bool:
    """Whether any sales source may be used in this espace."""
    return any(entry.available() for entry in sales_sources.SOURCES.values())


def _refusal() -> str:
    """Why no source may be used here: the first source's sentence."""
    entry = next(iter(sales_sources.SOURCES.values()), None)
    return entry.unavailable_reason() if entry is not None else ""


def _may_change(request) -> None:
    """A POST changing the rules: the espace's owner, where a source may be
    used - anybody else gets the 403 page."""
    if not _available() or not is_owner(request):
        raise PermissionDenied


def _new_initial() -> dict:
    """The new rule, pre-filled: yesterday's sales every morning."""
    return {
        "name": "Ventes de la veille",
        "source": sales_sources.LADDITION,
        "weekdays": [str(day) for day in range(7)],
        "times": "07:00",
        "is_active": True,
    }


def _card(rule: AutoSalesImport, form, runs, now) -> dict:
    """A rule's card. A row the admin or a hand left unreadable (its days,
    its times, a source no longer offered) is said on the card, never a 500
    for every viewer."""
    problem = ""
    try:
        days = rule.weekday_list()
    except ValueError:
        days = ()
        problem = "jours illisibles : corrigez-les"
    try:
        times = rule.time_list()
    except ValueError:
        times = ()
        problem = "heures illisibles : corrigez-les"
    entry = sales_sources.source(rule.source)
    if entry is None:
        problem = problem or "source inconnue : choisissez-en une"
    upcoming = []
    if rule.is_active and days and times and not problem:
        upcoming = [schedule.describe_gather_instant(instant) for instant in auto_sales.next_slots(rule, now)]
    return {
        "rule": rule,
        "form": form,
        "source_said": entry.label if entry is not None else f"{rule.source} (plus proposée)",
        "days_said": schedule.format_weekdays(days) if days else "—",
        "times_said": schedule.format_times(times) if times else "—",
        "problem": problem,
        "upcoming": upcoming,
        "runs": runs,
    }


def _coverage_lines() -> list[str]:
    """How far each available source's sales are imported without a gap."""
    lines = []
    for entry in sales_sources.SOURCES.values():
        if not entry.available():
            continue
        until = auto_sales.covered_until(entry.key)
        if until is None:
            lines.append(f"{entry.label} : aucun import réussi pour l'instant.")
        else:
            lines.append(f"{entry.label} : ventes importées sans trou jusqu'au {until:%d/%m/%Y}.")
    return lines


def _page(request, bound=None, new_form=None, status=200):
    """The page; `bound` is (pk, form) - a rule's refused form, drawn back in
    its own card - and `new_form` the new rule's."""
    owner = is_owner(request)
    context = {
        "refused": "" if _available() else _refusal(),
        "owner": owner,
        "owner_only": "" if owner else OWNER_ONLY_SETTINGS,
        "cards": [],
        "new_form": None,
        "sending_enabled": webpush.sending_enabled(),
        "head_fields": HEAD_FIELDS,
        "tail_fields": TAIL_FIELDS,
        "overlap_days": auto_sales.OVERLAP_DAYS,
        "night_ends_at": auto_sales.night_ends_at(),
        "catch_up_hours": int(auto_sales.CATCH_UP_LIMIT.total_seconds() // 3600),
        "coverage": [],
    }
    if context["refused"]:
        return render(request, "recipes/auto_sales.html", context, status=status)

    now = timezone.now()
    context["coverage"] = _coverage_lines()
    for rule in AutoSalesImport.objects.order_by("pk"):
        form = None
        if owner:
            if bound is not None and bound[0] == rule.pk:
                form = bound[1]
            else:
                form = AutoSalesImportForm(prefix=f"import-{rule.pk}", initial=AutoSalesImportForm.initial_for(rule))
        runs = list(
            SalesImportJob.objects.filter(trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=rule.pk)
            .defer("log")
            .order_by("-started_at", "-pk")[:AUTO_SALES_RUNS_SHOWN]
        )
        context["cards"].append(_card(rule, form, runs, now))
    if owner:
        context["new_form"] = new_form or AutoSalesImportForm(prefix=NEW_AUTO_SALES, initial=_new_initial())
    return render(request, "recipes/auto_sales.html", context, status=status)


def auto_sales_page(request):
    """« Import automatique des ventes »: the rules, and a new one posted
    here."""
    if request.method != "POST":
        return _page(request)
    _may_change(request)
    form = AutoSalesImportForm(request.POST, prefix=NEW_AUTO_SALES)
    if form.is_valid() and AutoSalesImport.objects.count() >= AutoSalesImport.MAX_PER_TENANT:
        form.add_error("name", AUTO_SALES_CAP)
    if not form.is_valid():
        return _page(request, new_form=form)
    rule = AutoSalesImport.objects.create(**form.values())
    # From now on: « Enregistrer » never runs a slot already past.
    AutoSalesImport.objects.filter(pk=rule.pk).update(last_slot_at=timezone.now())
    messages.success(request, f"Import automatique « {rule.name} » enregistré.")
    return redirect(_page_url(rule.pk))


def auto_sales_edit(request, pk):
    if request.method != "POST":
        return redirect(_page_url(pk))
    _may_change(request)
    rule = get_object_or_404(AutoSalesImport, pk=pk)
    form = AutoSalesImportForm(request.POST, prefix=f"import-{rule.pk}")
    if not form.is_valid():
        return _page(request, bound=(rule.pk, form))
    values = form.values()
    # Switched back on, or its days or times changed: its slots count from
    # now, never one already past.
    restart = (values["is_active"] and not rule.is_active) or any(
        values[name] != getattr(rule, name) for name in ("weekdays", "times")
    )
    for name, value in values.items():
        setattr(rule, name, value)
    # The form's fields only: the scheduler's (last_slot_at, last_result,
    # last_failed) are written by it alone.
    rule.save(update_fields=[*values, "updated_at"])
    if restart:
        AutoSalesImport.objects.filter(pk=rule.pk).update(last_slot_at=timezone.now())
    messages.success(request, f"Import automatique « {rule.name} » enregistré.")
    return redirect(_page_url(rule.pk))


def auto_sales_delete(request, pk):
    if request.method != "POST":
        return redirect(_page_url(pk))
    _may_change(request)
    rule = get_object_or_404(AutoSalesImport, pk=pk)
    name = rule.name
    rule.delete()
    messages.success(request, f"Import automatique « {name} » supprimé.")
    return redirect(_page_url())
