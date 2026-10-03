"""Sending the queued notifications: which devices (`devices_for`), the
delivery thread (`deliver_pending`, `start_delivery`), one dispatch
(`deliver_one`) and « Envoyer un essai » (`send_test`).

**Never in the scheduler's thread, never inside a transaction.** A job or an
event inserts `pending` rows; a delivery thread of the espace (at most one
alive per espace, `start_delivery`) claims each one with a conditional
UPDATE (`pending → sending`) and sends - a push service may take seconds per
device, and one bar's devices must not hold the 00:00 reminders of another.

**Who receives**: the espace's members whose login is active, their devices
neither tombstoned (404/410), nor logged out, nor subscribed under another
server key (`server_key` is what the browser said it subscribed with) - at
most `MAX_DEVICES_PER_DISPATCH`. A device's statistics are written in their
own `except DatabaseError`: a locked accounts database never costs the
dispatch's result.

**Nothing is raised to a caller**: an unexpected error makes the dispatch
`failed` « erreur interne », logged with its traceback (our own code: never
a requests exception, whose text holds an endpoint - webpush catches those).
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, OperationalError
from django.db.models import F
from django.utils import timezone

from accounts.models import Membership, PushDevice
from accounts.tenancy import bound, require_tenant, tenant_key

from . import webpush
from .models import Dispatch, clean_ids, clip, create_dispatch, test_key

logger = logging.getLogger(__name__)

MAX_DEVICES_PER_DISPATCH = 50
#: « Envoyer un essai »: one per login and espace this often.
TEST_INTERVAL_SECONDS = 30

NO_DEVICE = "aucun appareil inscrit"
INTERNAL_ERROR = "erreur interne"
EXPIRED = "expiré avant l'envoi"
INTERRUPTED = "interrompu (serveur redémarré)"
#: A dispatch still `sending` this long after its claim was abandoned.
SENDING_STALE_AFTER = timedelta(minutes=10)

TEST_DISABLED = "Envois désactivés sur ce serveur (serveur de développement)."
TEST_TOO_SOON = "Patientez quelques secondes avant un nouvel essai."
TEST_NO_DEVICE = "Aucun appareil inscrit."
TEST_DEVICE_STALE = "Cet appareil est à réactiver : touchez « Activer » sur lui."
TEST_RULE_NAME = "Essai"


def _devices_of_espace():
    """The current espace's devices of active logins (the tenant read once:
    `require_tenant`)."""
    tenant = require_tenant()
    return PushDevice.objects.filter(membership__tenant_id=tenant.pk, membership__user__is_active=True)


def _selectable(devices):
    return devices.filter(gone_at__isnull=True, logged_out_at__isnull=True, server_key=webpush.vapid_public_key())


def devices_for(recipient_ids=None) -> list[PushDevice]:
    """The devices a dispatch for `recipient_ids` (user ids; None: every
    member) goes to, in the current espace - the last seen first, at most
    MAX_DEVICES_PER_DISPATCH."""
    devices = _devices_of_espace()
    if recipient_ids is not None:
        devices = devices.filter(membership__user_id__in=clean_ids(recipient_ids))
    return list(_selectable(devices).order_by("-seen_at", "-pk")[:MAX_DEVICES_PER_DISPATCH])


def stale_count(recipient_ids=None) -> int:
    """The recipients' devices to reactivate: tombstoned or subscribed under
    another key (a logged-out one is not counted: its login left)."""
    devices = _devices_of_espace().filter(logged_out_at__isnull=True)
    if recipient_ids is not None:
        devices = devices.filter(membership__user_id__in=clean_ids(recipient_ids))
    return devices.count() - _selectable(devices).count()


# -- The delivery thread ----------------------------------------------------------------------------------------------

_threads_lock = threading.Lock()
#: tenant_key() -> the espace's delivery thread. Threads, never rows.
_delivery_threads: dict[str, threading.Thread] = {}
#: The espaces whose delivery was asked for while their thread was alive:
#: that thread looks once more before it ends (no wake-up lost while it
#: finishes). Both read and written under `_threads_lock`.
_again: set[str] = set()


def start_delivery() -> bool:
    """Start the current espace's delivery thread unless one is alive - then
    that one looks again before it ends. Called bound (the scheduler's tick,
    an event's on-commit). Whether one was started."""
    key = tenant_key()
    with _threads_lock:
        running = _delivery_threads.get(key)
        if running is not None and running.is_alive() is True:
            _again.add(key)
            return False
        _again.discard(key)
        thread = threading.Thread(target=bound(deliver_pending), name=f"marginmate-notifications-{key}", daemon=True)
        _delivery_threads[key] = thread
    thread.start()
    return True


def deliver_pending() -> int:
    """Send every pending dispatch of the bound espace, oldest first: each
    claimed (`pending → sending`) before it is sent, so two threads never
    send one twice. How many were delivered by this thread.

    Its exit is decided under `_threads_lock`: a delivery asked for after
    its last query found nothing makes it look again, and once it decides
    to end it leaves its place, so the next `start_delivery` starts a new
    thread rather than counting on one tearing down."""
    key = tenant_key()
    handled = 0
    try:
        while True:
            pk = (
                Dispatch.objects.filter(status=Dispatch.Status.PENDING)
                .order_by("created_at", "pk")
                .values_list("pk", flat=True)
                .first()
            )
            if pk is None:
                with _threads_lock:
                    if key in _again:
                        _again.discard(key)
                        continue
                    if _delivery_threads.get(key) is threading.current_thread():
                        del _delivery_threads[key]
                return handled
            claimed = Dispatch.objects.filter(pk=pk, status=Dispatch.Status.PENDING).update(
                status=Dispatch.Status.SENDING, sent_at=timezone.now()
            )
            if claimed:
                deliver_one(Dispatch.objects.get(pk=pk))
                handled += 1
    except DatabaseError:
        logger.exception("Notifications : envoi interrompu (base de données indisponible)")
    return handled


def _expired(dispatch: Dispatch, now) -> bool:
    return now - dispatch.created_at > timedelta(seconds=max(int(dispatch.ttl or 0), 1))


def _say(delivered: int, total: int) -> str:
    """« 1 appareil sur 1 », « 2 appareils sur 3 »."""
    return f"{delivered} appareil{'s' if delivered > 1 else ''} sur {total}"


def deliver_one(dispatch: Dispatch, *, retry=True, devices=None) -> Dispatch:
    """Send one claimed dispatch and write its result (status, devices,
    delivered, detail, sent_at). `devices`: these, rather than
    `devices_for` its recipients (an « essai »). Never raises."""
    try:
        return _deliver(dispatch, retry=retry, devices=devices)
    except Exception:  # our own bug becomes the dispatch's « erreur interne », never the caller's
        logger.exception("Notification n° %s non envoyée : erreur interne", dispatch.pk)
        _finish(dispatch, Dispatch.Status.FAILED, INTERNAL_ERROR)
        return dispatch


def _deliver(dispatch: Dispatch, *, retry: bool, devices) -> Dispatch:
    now = timezone.now()
    if not webpush.sending_enabled():
        return _finish(dispatch, Dispatch.Status.DISABLED, webpush.DISABLED)
    if _expired(dispatch, now):
        return _finish(dispatch, Dispatch.Status.FAILED, EXPIRED)
    if devices is None:
        devices = devices_for(dispatch.recipient_ids)
    if not devices:
        stale = stale_count(dispatch.recipient_ids)
        detail = f"{NO_DEVICE} ({stale} à réactiver)" if stale else NO_DEVICE
        return _finish(dispatch, Dispatch.Status.NO_DEVICE, detail)
    payload = webpush.payload_for(
        dispatch.title, dispatch.body, str(settings.SITE_URL or "") + (dispatch.target or "/"), f"d{dispatch.pk}"
    )
    delivered, errors = 0, []
    for device in devices:
        result = webpush.send(
            device.endpoint,
            device.p256dh,
            device.auth,
            payload,
            ttl=dispatch.ttl,
            topic=dispatch.topic,
            retry=retry,
        )
        _record(device, result)
        if result.ok:
            delivered += 1
        elif result.error and result.error not in errors:
            errors.append(result.error)
    total = len(devices)
    if delivered == total:
        status = Dispatch.Status.SENT
    elif delivered:
        status = Dispatch.Status.PARTIAL
    else:
        status = Dispatch.Status.FAILED
    detail = _say(delivered, total)
    if errors:
        detail = f"{detail} — {', '.join(errors)}"
    return _finish(dispatch, status, detail, devices=total, delivered=delivered)


def _record(device: PushDevice, result: webpush.PushResult) -> None:
    """A device's statistics after one send; a failure to write them is
    logged, never the dispatch's."""
    now = timezone.now()
    try:
        devices = PushDevice.objects.filter(pk=device.pk)
        if result.ok:
            devices.update(last_success_at=now, failures=0, last_error="")
        else:
            fields = {"last_error_at": now, "last_error": clip(result.error, 200), "failures": F("failures") + 1}
            if result.gone:
                fields["gone_at"] = now
            devices.update(**fields)
    except DatabaseError:
        logger.warning("Notifications : statistiques non écrites pour %s", webpush._where(device.endpoint))


def _finish(dispatch: Dispatch, status, detail: str, *, devices=0, delivered=0) -> Dispatch:
    """The dispatch's result, written once - again once on an OperationalError
    (a database locked by an import)."""
    dispatch.status = status
    dispatch.detail = clip(detail, 300)
    dispatch.devices = devices
    dispatch.delivered = delivered
    dispatch.sent_at = timezone.now()
    fields = {
        "status": dispatch.status,
        "detail": dispatch.detail,
        "devices": devices,
        "delivered": delivered,
        "sent_at": dispatch.sent_at,
    }
    for attempt in range(2):
        try:
            Dispatch.objects.filter(pk=dispatch.pk).update(**fields)
            break
        except OperationalError:
            if attempt:
                logger.exception("Notification n° %s : résultat non enregistré", dispatch.pk)
    return dispatch


# -- « Envoyer un essai » ---------------------------------------------------------------------------------------------


def _test_cache_key(membership: Membership) -> str:
    return f"notifications:essai:{tenant_key()}:{membership.user_id}"


def send_test(membership: Membership, *, title, body, target, device=None) -> tuple[int, int, str]:
    """« Envoyer un essai », now, in the request's thread: these texts to the
    login's devices (or to `device` alone), no retry. (sent, total, error) -
    `error` a French sentence when nothing went, "" otherwise. One per login
    and espace every TEST_INTERVAL_SECONDS. Recorded in the history."""
    if not webpush.sending_enabled():
        return 0, 0, TEST_DISABLED
    if not cache.add(_test_cache_key(membership), 1, timeout=TEST_INTERVAL_SECONDS):
        return 0, 0, TEST_TOO_SOON
    mine = _selectable(PushDevice.objects.filter(membership=membership)).order_by("-seen_at", "-pk")
    if device is not None:
        chosen = list(mine.filter(pk=device.pk))
        if not chosen:
            return 0, 0, TEST_DEVICE_STALE
    else:
        chosen = list(mine[:MAX_DEVICES_PER_DISPATCH])
    if not chosen:
        return 0, 0, TEST_NO_DEVICE
    dispatch = create_dispatch(
        kind=Dispatch.Kind.TEST,
        rule_name=TEST_RULE_NAME,
        dedupe_key=test_key(),
        title=title,
        body=body,
        target=target,
        ttl=webpush.TTL_TEST,
        recipient_ids=[membership.user_id],
        status=Dispatch.Status.SENDING,
        sent_at=timezone.now(),
    )
    if dispatch is None:  # a uuid4 key already there: never in practice
        return 0, len(chosen), f"Essai non reçu ({INTERNAL_ERROR})."
    deliver_one(dispatch, retry=False, devices=chosen)
    if dispatch.delivered:
        return dispatch.delivered, dispatch.devices, ""
    return 0, dispatch.devices or len(chosen), f"Essai non reçu ({dispatch.detail})."
