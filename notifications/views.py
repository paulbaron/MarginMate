"""The notifications' pages and the browser's files.

- « Notifications » (/notifications/): the server's state, « Cet appareil »
  (drawn by static/js/notifications.js: every state is in the page, hidden,
  the script shows one), the member's devices, the rules in short and the
  latest dispatches.
- « Rappels » (/notifications/rappels/) and « Alertes »
  (/notifications/evenements/): one card and one form per rule, each posting
  to its own address with its card's fragment (returnables' types, the
  precedent). A refused form is drawn back at 200 in its card; « Aperçu »
  (the form's first, hidden submit: Enter previews) draws the posted values
  and their next sends and saves nothing.
- The device endpoints (JSON in and out, CSRF through X-CSRFToken): the key,
  « Activer » (`subscribe`), the pages' sync, « Retirer », « Envoyer un
  essai ». They only wrap notifications/devices.py and sending.send_test.
- /sw.js and /manifest.webmanifest: public (a service worker's script must
  never be a redirect to the login, a manifest is fetched without the
  session), unbound - they read no espace.

**Who may do what** (spec §1.6): the rules are the espace owner's. The gate
(accounts/access.py) refuses their pages to an employee; behind it, these
views still draw them read-only for a member and answer his POST with the
403 page (defence in depth). The devices are each login's own, and their
routes every login's: the membership is always the request's login in the
espace the request is bound to. On « Notifications » a member is drawn his
devices only.

A GET on a POST-only route goes back to its page and writes nothing.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from accounts.access import access_of
from accounts.models import PushDevice
from accounts.tenancy import integrations_allowed, is_owner
from common import is_id

from . import devices, registry, schedule, sending, webpush
from .forms import NEW_REMINDER, NEW_REMINDER_HELP, EventForm, NightForm, ReminderForm, active_members
from .models import Dispatch, EventRule, NotificationSettings, Reminder

logger = logging.getLogger(__name__)

OWNER_ONLY = "Seul le propriétaire de l'espace modifie ces réglages."
SERVER_DISABLED = "Envois désactivés sur ce serveur (serveur de développement)."
SCHEDULER_NEVER = "Le planificateur ne tourne pas : les rappels ne partent pas."
#: The scheduler ticks every minute: past this, it is said stopped.
SCHEDULER_LATE = timedelta(minutes=3)
NOT_REGISTERED = "Activé dans le navigateur mais pas enregistré : réessayez."
BAD_REQUEST = "Demande illisible."
TOO_BIG = "Demande trop longue."
#: A device endpoint's body: a subscription is some 2 KB at most.
MAX_BODY_BYTES = 4096
#: « Derniers envois ».
HISTORY_ROWS = 30
TEST_TITLE = "Essai MarginMate"
TEST_BODY = "Les notifications arrivent sur cet appareil."
DEVICE_REMOVED = "Appareil retiré."
#: The dispatch statuses' pills (marginmate.css, « notifications »).
STATUS_CSS = {status: f"status-{status}" for status in Dispatch.Status.values}

SERVICE_WORKER = Path(__file__).with_name("service_worker.js")
THEME_COLOR = "#1a1a21"
BACKGROUND_COLOR = "#131318"


# -- Shared ---------------------------------------------------------------------------------------------------------


def _owner_only(request) -> None:
    """A rule's POST: the espace owner's, the 403 page for anybody else."""
    if not is_owner(request):
        raise PermissionDenied(OWNER_ONLY)


def _settings() -> NotificationSettings:
    """The espace's settings as stored - never written by a GET (a fresh
    espace reads the defaults)."""
    return NotificationSettings.objects.filter(pk=NotificationSettings.SINGLETON_PK).first() or NotificationSettings()


def _members(request) -> list[tuple[int, str]]:
    return active_members(request.tenant.pk)


def _suppliers(keep=None) -> list[tuple[str, str]]:
    """« Repris par »: the suppliers a pickup may name (returnables' rule)."""
    from returnables.forms import supplier_choices

    return [(str(pk), name) for pk, name in supplier_choices(keep=keep).values_list("pk", "name")]


def _supplier_name(supplier_id) -> str:
    if not supplier_id:
        return ""
    from invoices.models import Supplier

    return Supplier.objects.filter(pk=supplier_id).values_list("name", flat=True).first() or ""


def _url(name: str, fragment: str = "") -> str:
    return reverse(name) + (f"#{fragment}" if fragment else "")


def _auto_gathers_url() -> str:
    """The automatic gathers' page (invoices), "" while it does not exist."""
    try:
        return reverse("invoices:auto_gathers")
    except NoReverseMatch:
        return ""


def _auto_sales_url() -> str:
    """The automatic sales imports' page (recipes), "" while it does not
    exist."""
    try:
        return reverse("recipes:auto_sales")
    except NoReverseMatch:
        return ""


def _previews(weekdays, times, night_ends_at, *, skip_if="", skip_hours=0, supplier_name="", now=None) -> list[str]:
    """« Prochains envois »: the next five instants, described, each with
    what would skip it."""
    now = now or timezone.now()
    if not weekdays or not times:
        return []
    condition = registry.skip_condition(skip_if)
    lines = []
    for instant in schedule.next_reminder_instants(weekdays, times, night_ends_at, now):
        line = schedule.describe_reminder_instant(instant, night_ends_at)
        if condition is not None:
            line = f"{line} · {condition.preview(instant - timedelta(hours=skip_hours), supplier_name)}"
        lines.append(line)
    return lines


def _saved_previews(reminder: Reminder, night_ends_at, now=None) -> list[str]:
    try:
        weekdays, times = reminder.weekday_list(), reminder.time_list()
    except ValueError:
        return []
    return _previews(
        weekdays,
        times,
        night_ends_at,
        skip_if=reminder.skip_if,
        skip_hours=reminder.skip_hours,
        supplier_name=_supplier_name(reminder.skip_supplier_id),
        now=now,
    )


def _next_send(reminder: Reminder, night_ends_at, now):
    """The reminder's next UTC instant, None when it has none."""
    try:
        instants = schedule.next_reminder_instants(
            reminder.weekday_list(), reminder.time_list(), night_ends_at, now, count=1
        )
    except ValueError:
        return None
    return instants[0] if instants else None


def _server_line(settings_row: NotificationSettings, now) -> tuple[str, str]:
    """The page's first line and its message class."""
    if not webpush.sending_enabled():
        return SERVER_DISABLED, "warning"
    last = settings_row.last_tick_at
    if last is None:
        return SCHEDULER_NEVER, "error"
    local = timezone.localtime(last)
    if now - last > SCHEDULER_LATE:
        return (
            f"Le planificateur ne tourne pas depuis le {local:%d/%m} à {local:%H:%M} : les rappels ne partent pas.",
            "error",
        )
    return f"Planificateur actif (dernier passage à {local:%H:%M}).", "success"


# -- « Notifications » ----------------------------------------------------------------------------------------------


def home(request):
    """« Notifications ». Every login opens it for its own devices
    (accounts/access.py); the rules in short, the links to the automatic
    runs and « Derniers envois » are the owner's - a member is drawn
    « Cet appareil » and « Mes appareils » only, nothing else read."""
    membership = devices.membership_of(request)
    key = webpush.vapid_public_key()
    mine = devices.devices_of(membership) if membership is not None else []
    named = devices.cookie_device(request, membership) if membership is not None else None
    push_config = {
        "key": key,
        "subscribe": reverse("notifications:subscribe"),
        "sync": reverse("notifications:sync"),
        "test": reverse("notifications:test"),
        "remove": reverse("notifications:device_delete", args=[0]),
        "device": named.pk if named is not None else None,
    }
    device_rows = [(device, devices.needs_renewal(device, key)) for device in mine]
    # The gate's access first (no query, False for a member), the owner's
    # row second: defence in depth, as every rule's POST (_owner_only).
    if not (access_of(request).owner and is_owner(request)):
        return render(
            request,
            "notifications/home.html",
            {"owner": False, "push_config": push_config, "devices": device_rows},
        )

    now = timezone.now()
    settings_row = _settings()
    active = list(Reminder.objects.filter(is_active=True))
    upcoming = [
        (instant, reminder)
        for reminder in active
        if (instant := _next_send(reminder, settings_row.night_ends_at, now)) is not None
    ]
    upcoming.sort(key=lambda pair: (pair[0], pair[1].pk))
    next_send = (
        f"{upcoming[0][1].name} : {schedule.describe_reminder_instant(upcoming[0][0], settings_row.night_ends_at)}"
        if upcoming
        else ""
    )
    alerts = []
    for rule in EventRule.objects.filter(is_active=True):
        event = registry.event(rule.event)
        if event is not None:
            alerts.append(event.label)

    server_text, server_css = _server_line(settings_row, now)
    history = [(dispatch, STATUS_CSS.get(dispatch.status, "")) for dispatch in Dispatch.objects.all()[:HISTORY_ROWS]]
    context = {
        "owner": True,
        "server_text": server_text,
        "server_css": server_css,
        "push_config": push_config,
        "devices": device_rows,
        "reminder_count": len(active),
        "next_send": next_send,
        "alerts": alerts,
        "auto_gathers_url": _auto_gathers_url() if integrations_allowed() else "",
        # The till's account is the owner's too (recipes/integration.py).
        "auto_sales_url": _auto_sales_url() if integrations_allowed() else "",
        "history": history,
    }
    return render(request, "notifications/home.html", context)


# -- « Rappels » ----------------------------------------------------------------------------------------------------


def _reminder_card(reminder, form, *, night_ends_at, owner, previewed=False, now=None) -> dict:
    anchor = f"rappel-{reminder.pk}" if reminder is not None else "nouveau-rappel"
    if form is not None and form.is_bound and form.is_valid():
        data = form.cleaned_data
        previews = _previews(
            data["days"],
            data["times"],
            night_ends_at,
            skip_if=data["skip_if"],
            skip_hours=data["skip_hours"],
            supplier_name=_supplier_name(data["skip_supplier_id"]),
            now=now,
        )
        active = data["is_active"]
    elif reminder is not None:
        previews = _saved_previews(reminder, night_ends_at, now)
        active = reminder.is_active
    else:
        previews, active = [], True
    action = (
        reverse("notifications:reminder_edit", args=[reminder.pk])
        if reminder is not None
        else reverse("notifications:reminders")
    )
    return {
        "reminder": reminder,
        "form": form if owner else None,
        "parts": form.parts() if owner and form is not None else (),
        "days": form.day_boxes(night_ends_at) if owner and form is not None else (),
        "anchor": anchor,
        "action": action,
        "previews": previews,
        "active": active,
        "previewed": previewed,
        "summary": _reminder_summary(reminder, night_ends_at) if reminder is not None else "",
    }


def _reminder_summary(reminder: Reminder, night_ends_at) -> str:
    """A member's read-only line: « lun., mer. et sam. soir à 00:00 et
    02:00 »."""
    try:
        days, times = reminder.weekday_list(), reminder.time_list()
    except ValueError:
        return ""
    evening = "" if night_ends_at == schedule.CALENDAR else " soir"
    return f"{schedule.format_weekdays(days)}{evening} à {schedule.format_times(times)}"


def _reminders_page(request, *, bound=None, new_form=None, night_form=None, previewed=None, status=200):
    owner = is_owner(request)
    settings_row = _settings()
    night = settings_row.night_ends_at
    members = _members(request)
    now = timezone.now()
    cards = []
    for reminder in Reminder.objects.all():
        if bound is not None and bound.instance is not None and bound.instance.pk == reminder.pk:
            form = bound
        elif owner:
            form = ReminderForm(
                request=request,
                instance=reminder,
                members=members,
                suppliers=_suppliers(keep=reminder.skip_supplier_id),
                prefix=f"rappel-{reminder.pk}",
            )
        else:
            form = None
        cards.append(
            _reminder_card(
                reminder, form, night_ends_at=night, owner=owner, previewed=previewed == reminder.pk, now=now
            )
        )
    new_card = None
    if owner:
        if new_form is None:
            new_form = ReminderForm(
                request=request, members=members, suppliers=_suppliers(), prefix="nouveau", initial=_new_initial()
            )
        new_card = _reminder_card(
            None, new_form, night_ends_at=night, owner=owner, previewed=previewed == "nouveau", now=now
        )
    if night_form is None:
        night_form = NightForm(initial={"night_ends_at": f"{night:%H:%M}"})
    context = {
        "owner": owner,
        "owner_only": OWNER_ONLY,
        "night": night,
        "night_form": night_form if owner else None,
        "days_legend": "Jours"
        if night == schedule.CALENDAR
        else "Soirs (la nuit compte pour le soir où elle commence)",
        "cards": cards,
        "new_card": new_card,
        "new_help": NEW_REMINDER_HELP,
    }
    return render(request, "notifications/reminders.html", context, status=status)


def _new_initial() -> dict:
    initial = dict(NEW_REMINDER)
    if registry.page_target(initial["page"]) == "":
        initial["page"] = registry.OTHER_PAGE
    return initial


def _previewing(request) -> bool:
    return request.POST.get("action") == "apercu"


def reminders(request):
    if request.method != "POST":
        return _reminders_page(request)
    _owner_only(request)
    form = ReminderForm(
        request.POST, request=request, members=_members(request), suppliers=_suppliers(), prefix="nouveau"
    )
    if _previewing(request):
        form.is_valid()
        return _reminders_page(request, new_form=form, previewed="nouveau")
    if form.is_valid():
        reminder = form.save()
        messages.success(request, f"Rappel « {reminder.name} » enregistré.")
        return redirect(_url("notifications:reminders", f"rappel-{reminder.pk}"))
    return _reminders_page(request, new_form=form)


def reminder_edit(request, pk):
    reminder = get_object_or_404(Reminder, pk=pk)
    if request.method != "POST":
        return redirect(_url("notifications:reminders", f"rappel-{reminder.pk}"))
    _owner_only(request)
    form = ReminderForm(
        request.POST,
        request=request,
        instance=reminder,
        members=_members(request),
        suppliers=_suppliers(keep=reminder.skip_supplier_id),
        prefix=f"rappel-{reminder.pk}",
    )
    if _previewing(request):
        form.is_valid()
        return _reminders_page(request, bound=form, previewed=reminder.pk)
    if form.is_valid():
        saved = form.save()
        messages.success(request, f"Rappel « {saved.name} » enregistré.")
        return redirect(_url("notifications:reminders", f"rappel-{saved.pk}"))
    return _reminders_page(request, bound=form)


def reminder_delete(request, pk):
    reminder = get_object_or_404(Reminder, pk=pk)
    if request.method != "POST":
        return redirect(_url("notifications:reminders", f"rappel-{reminder.pk}"))
    _owner_only(request)
    name = reminder.name
    reminder.delete()
    messages.success(request, f"Rappel « {name} » supprimé.")
    return redirect("notifications:reminders")


def night(request):
    if request.method != "POST":
        return redirect(_url("notifications:reminders", "nuit"))
    _owner_only(request)
    form = NightForm(request.POST)
    if not form.is_valid():
        return _reminders_page(request, night_form=form)
    ends_at = form.cleaned_data["night_ends_at"]
    settings_row = NotificationSettings.get_solo()
    # Its one column: last_tick_at is the scheduler's.
    NotificationSettings.objects.filter(pk=settings_row.pk).update(night_ends_at=ends_at)
    now = timezone.now()
    sends = []
    for reminder in Reminder.objects.filter(is_active=True):
        instant = _next_send(reminder, ends_at, now)
        if instant is not None:
            sends.append(f"{reminder.name} : {schedule.describe_reminder_instant(instant, ends_at)}")
    said = f"La nuit se termine à {ends_at:%H:%M}."
    if sends:
        said = f"{said} Prochains envois : {' ; '.join(sends)}."
    messages.success(request, said)
    return redirect(_url("notifications:reminders", "nuit"))


# -- « Alertes » ----------------------------------------------------------------------------------------------------


def _events_page(request, *, bound=None, status=200):
    owner = is_owner(request)
    members = _members(request)
    rules = {rule.event: rule for rule in EventRule.objects.all()}
    cards = []
    for event in registry.EVENTS.values():
        rule = rules.get(event.key)
        if bound is not None and bound.event.key == event.key:
            form = bound
        elif owner:
            form = EventForm(request=request, event=event, rule=rule, members=members, prefix=f"alerte-{event.key}")
        else:
            form = None
        cards.append(
            {
                "event": event,
                "rule": rule,
                "form": form,
                "parts": form.parts() if form is not None else (),
                "outcomes": form.outcome_boxes() if form is not None else (),
                "anchor": f"alerte-{event.key}",
                "active": rule is not None and rule.is_active,
                "chosen": [event.outcome_label(key) for key in rule.outcomes if isinstance(key, str)]
                if rule is not None and isinstance(rule.outcomes, list)
                else [],
            }
        )
    context = {
        "owner": owner,
        "owner_only": OWNER_ONLY,
        "cards": cards,
        "consignes": registry.RETURNABLES_COMPARISON,
        "gather": registry.INVOICES_AUTO_GATHER,
        "sales": registry.RECIPES_AUTO_SALES,
    }
    return render(request, "notifications/events.html", context, status=status)


def events(request):
    return _events_page(request)


def event_edit(request, event):
    found = registry.event(event)
    if found is None:
        raise Http404("Événement inconnu.")
    if request.method != "POST":
        return redirect(_url("notifications:events", f"alerte-{found.key}"))
    _owner_only(request)
    form = EventForm(
        request.POST,
        request=request,
        event=found,
        rule=EventRule.objects.filter(event=found.key).first(),
        members=_members(request),
        prefix=f"alerte-{found.key}",
    )
    if form.is_valid():
        form.save()
        messages.success(request, f"Alerte « {found.label} » enregistrée.")
        return redirect(_url("notifications:events", f"alerte-{found.key}"))
    return _events_page(request, bound=form)


# -- The devices ----------------------------------------------------------------------------------------------------


class _Unreadable(ValueError):
    """A device endpoint's body that is no JSON object (or too long): 400."""


def _json_body(request) -> dict:
    # Before the body is touched: a multipart body was read from the stream
    # by the CSRF middleware, and `request.body` would raise (a 500). The
    # scripts always post application/json.
    if not _is_json(request):
        raise _Unreadable(BAD_REQUEST)
    length = request.META.get("CONTENT_LENGTH") or "0"
    if is_id(length) and int(length) > MAX_BODY_BYTES:
        raise _Unreadable(TOO_BIG)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        raise _Unreadable(TOO_BIG)
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise _Unreadable(BAD_REQUEST) from None
    if not isinstance(data, dict):
        raise _Unreadable(BAD_REQUEST)
    return data


def _refused(error: str, status=400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": error}, status=status)


def _is_json(request) -> bool:
    return request.content_type == "application/json"


def _subscription(data: dict) -> dict:
    """endpoint, p256dh, auth and server_key as posted: strings or None."""
    keys = data.get("keys")
    keys = keys if isinstance(keys, dict) else {}
    values = {
        "endpoint": data.get("endpoint"),
        "p256dh": keys.get("p256dh"),
        "auth": keys.get("auth"),
        "server_key": data.get("server_key"),
    }
    for value in values.values():
        if value is not None and not isinstance(value, str):
            raise _Unreadable(BAD_REQUEST)
    return values


@never_cache
@require_safe
def key(request):
    """The server's public key (the browser subscribes with it) and whether
    this server sends at all."""
    return JsonResponse({"key": webpush.vapid_public_key(), "enabled": webpush.sending_enabled()})


def subscribe(request):
    """« Activer »: the one way a device row is created or moved here."""
    if request.method != "POST":
        return redirect(_url("notifications:home", "cet-appareil"))
    membership = devices.membership_of(request)
    if membership is None:
        return _refused(NOT_REGISTERED)
    try:
        data = _subscription(_json_body(request))
    except _Unreadable as refusal:
        return _refused(str(refusal))
    refusal = devices.origin_refusal(request)
    if refusal:
        return _refused(refusal)
    if data["endpoint"] is None:
        return _refused(BAD_REQUEST)
    try:
        device = devices.register(membership, user_agent=request.META.get("HTTP_USER_AGENT", ""), **data)
    except devices.DeviceRefused as refused:
        return _refused(str(refused))
    return devices.set_cookie(JsonResponse({"ok": True, "device": device.pk}), device)


def sync(request):
    """The pages' sync (static/js/push_sync.js): never creates a device."""
    if request.method != "POST":
        return redirect(_url("notifications:home", "cet-appareil"))
    membership = devices.membership_of(request)
    if membership is None:
        return JsonResponse({"state": devices.UNKNOWN})
    try:
        data = _subscription(_json_body(request))
    except _Unreadable as refusal:
        return _refused(str(refusal))
    try:
        synced = devices.sync(membership, cookie_device_pk=devices.cookie_device_pk(request), **data)
    except devices.DeviceRefused as refused:
        return _refused(str(refused))
    answer = {"state": synced.state}
    if synced.state == devices.OK and synced.device is not None:
        answer["device"] = synced.device.pk
    response = JsonResponse(answer)
    if synced.device is not None:
        devices.set_cookie(response, synced.device)
    return response


def device_delete(request, pk):
    """« Retirer » (a form, from « Mes appareils ») and « Désactiver sur cet
    appareil » (JSON, from the script): one of this login's devices."""
    if request.method != "POST":
        return redirect(_url("notifications:home", "cet-appareil"))
    membership = devices.membership_of(request)
    if membership is None or not devices.remove(membership, pk):
        raise Http404("Appareil inconnu.")
    named = devices.read_cookie(request)
    if _is_json(request):
        response = JsonResponse({"ok": True})
    else:
        messages.success(request, DEVICE_REMOVED)
        response = redirect(_url("notifications:home", "cet-appareil"))
    if named is not None and named[0] == pk:
        devices.clear_cookie(response)
    return response


def _sent_text(sent: int, total: int) -> str:
    if sent == total:
        return f"Essai envoyé à {sent} appareil{'s' if sent > 1 else ''}."
    return f"Essai envoyé à {sent} appareil{'s' if sent > 1 else ''} sur {total}."


def trial(request):
    """« Envoyer un essai »: to this login's devices (`appareil`: one of
    them), the texts of a reminder (`rappel`) or the essai's own."""
    if request.method != "POST":
        return redirect(_url("notifications:home", "cet-appareil"))
    as_json = _is_json(request)
    if as_json:
        try:
            data = _json_body(request)
        except _Unreadable as refusal:
            return _refused(str(refusal))
    else:
        data = request.POST
    membership = devices.membership_of(request)
    if membership is None:
        raise Http404("Aucun espace.")
    title, body, target = TEST_TITLE, TEST_BODY, reverse("notifications:home")
    back = _url("notifications:home", "cet-appareil")
    reminder_pk = str(data.get("rappel") or "")
    if reminder_pk:
        # A reminder's essai is on its card, the owner's (the route is every
        # login's for his own devices - accounts/access.py): a member never
        # reads a reminder's texts through it.
        _owner_only(request)
        if not is_id(reminder_pk):
            raise Http404("Rappel inconnu.")
        reminder = get_object_or_404(Reminder, pk=int(reminder_pk))
        title, body, target = reminder.title, reminder.body, reminder.target
        back = _url("notifications:reminders", f"rappel-{reminder.pk}")
    device = None
    device_pk = str(data.get("appareil") or "")
    if device_pk:
        if not is_id(device_pk):
            raise Http404("Appareil inconnu.")
        device = get_object_or_404(PushDevice, pk=int(device_pk), membership=membership)
    sent, total, error = sending.send_test(membership, title=title, body=body, target=target, device=device)
    said = error or _sent_text(sent, total)
    if as_json:
        return JsonResponse({"ok": not error, "message": said})
    (messages.error if error else messages.success)(request, said)
    return redirect(back)


# -- The browser's files --------------------------------------------------------------------------------------------


@functools.cache
def _service_worker_source() -> str:
    """Read once per process: the script changes with a deploy, which
    restarts the server."""
    return SERVICE_WORKER.read_text(encoding="utf-8")


@login_not_required
@require_safe
def service_worker(request):
    """/sw.js: the service worker, at the root so that its scope is the whole
    site. Public and unbound (an update check after the session ended must
    not get the login page: a redirected script is an error). no-cache: the
    browser asks again at every update check."""
    response = HttpResponse(_service_worker_source(), content_type="text/javascript; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response


@login_not_required
@require_safe
def manifest(request):
    """/manifest.webmanifest: what makes an iPhone's « Sur l'écran
    d'accueil » an app that may receive notifications (standalone). Public:
    a browser fetches it without the session."""
    icon = static("icons/icon-192.png")
    large = static("icons/icon-512.png")
    maskable = static("icons/icon-maskable-512.png")
    data = {
        "id": "/",
        "name": "MarginMate",
        "short_name": "MarginMate",
        "lang": "fr",
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": icon, "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": large, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": maskable, "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    response = JsonResponse(data, content_type="application/manifest+json", json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "max-age=3600"
    return response
