"""The browsers that receive the notifications (`accounts.PushDevice`): one
rule set for their whole life, which the HTTP views (notifications/views.py)
only wrap.

- **« Activer » is the only way a device row is CREATED or MOVED to another
  membership** (`register`, from a user's gesture). An endpoint already
  registered moves to the current membership only when the posted keys are
  the stored ones - whoever merely knows an endpoint (it sits in backups)
  cannot take a phone over. At most `MAX_DEVICES` per membership: the
  oldest seen goes.
- **The page's sync never creates** (`sync`): it finds the device by the
  signed cookie `marginmate_push` (« <device pk>:<user pk> ») when that row
  is this login's in this espace, else by its endpoint among this login's
  devices, and answers `ok` (refreshed), `renew` (the browser must subscribe
  again: no subscription, another server key, a tombstoned endpoint) or
  `unknown` (nothing of this login for this browser: « Activer » is the way).
  The sync of the fresh subscription a `renew` asked for brings a
  tombstoned device back (`gone_at` cleared, and the failures counted on
  its old endpoint forgotten).
- **A membership moved to another espace or given to another login loses
  its devices** (notifications/signals.py): a device serves one login of
  one espace.
- **A logout marks the cookie's device** (`mark_logged_out`): never sent to
  until the same login syncs again. Another login on the same browser
  deletes the previous login's device (notifications/signals.py).
- **Gone and failing devices are pruned by the tick** (`prune`): a
  tombstone after 90 days, a device failing 5 times in a row with no
  success for 7 days.

The membership is always `Membership(user=request.user, tenant=request.
tenant)` - the espace the request is bound to, never the user's first one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, router, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Membership, PushDevice

from . import webpush

COOKIE_NAME = "marginmate_push"
COOKIE_SALT = "notifications.device"
COOKIE_MAX_AGE = 400 * 24 * 3600
#: Devices per login and espace; past it the one seen longest ago goes.
MAX_DEVICES = 10
TOMBSTONE_DAYS = 90
FAILING_AFTER = 5
FAILING_DAYS = 7

ALREADY_REGISTERED = "Cet appareil est déjà inscrit."
KEY_CHANGED = "La clé du serveur a changé : rechargez la page, puis réactivez les notifications."
DEFAULT_LABEL = "Appareil"

OK = "ok"
RENEW = "renew"
UNKNOWN = "unknown"


class DeviceRefused(ValueError):
    """A subscription refused; its message is French (the page's error)."""


def membership_of(request) -> Membership | None:
    """The request's login in the espace it is bound to - None when none."""
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None or not user.is_authenticated:
        return None
    try:
        return Membership.objects.get(user=user, tenant_id=tenant.pk)
    except Membership.DoesNotExist:
        return None


# -- The cookie -------------------------------------------------------------------------------------------------------


def set_cookie(response, device: PushDevice):
    """Name `device` in this browser: HttpOnly, SameSite=Lax, Secure like the
    session cookie, 400 days. Returns `response`."""
    response.set_signed_cookie(
        COOKIE_NAME,
        f"{device.pk}:{device.membership.user_id}",
        salt=COOKIE_SALT,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=settings.SESSION_COOKIE_SECURE,
    )
    return response


def clear_cookie(response):
    response.delete_cookie(COOKIE_NAME, samesite="Lax")
    return response


def read_cookie(request) -> tuple[int, int] | None:
    """(device pk, user pk) the browser's cookie names, None when it has none
    or one whose signature fails. Never raises."""
    value = request.get_signed_cookie(COOKIE_NAME, default=None, salt=COOKIE_SALT, max_age=COOKIE_MAX_AGE)
    if not isinstance(value, str):
        return None
    device, _, user = value.partition(":")
    if not (device.isascii() and device.isdigit() and user.isascii() and user.isdigit()):
        return None
    if len(device) > 18 or len(user) > 18:
        return None
    return int(device), int(user)


def cookie_device_pk(request) -> int | None:
    """The device pk the cookie names when it names it for the request's
    own login - what `sync` takes as `cookie_device_pk`."""
    named = read_cookie(request)
    user = getattr(request, "user", None)
    if named is None or user is None or not user.is_authenticated or named[1] != user.pk:
        return None
    return named[0]


def cookie_device(request, membership: Membership) -> PushDevice | None:
    """The device the cookie names, when it is `membership`'s."""
    named = read_cookie(request)
    if named is None or named[1] != membership.user_id:
        return None
    return PushDevice.objects.filter(pk=named[0], membership=membership).first()


def mark_logged_out(request, now=None) -> int:
    """At logout, BEFORE the session is flushed: the cookie's device, when it
    is this login's, is sent nothing until the login syncs again. How many
    rows were marked (0 or 1). The cookie stays."""
    user = getattr(request, "user", None)
    named = read_cookie(request)
    if named is None or user is None or not user.is_authenticated or named[1] != user.pk:
        return 0
    return PushDevice.objects.filter(pk=named[0], membership__user_id=user.pk).update(
        logged_out_at=now or timezone.now()
    )


def forget_another_login_s_device(request, user) -> int:
    """At a login: the cookie naming ANOTHER login's device on this browser
    deletes that device (whoever enabled notifications here is no longer the
    one using it). The cookie itself stays; the next sync says `unknown`.
    How many rows went."""
    named = read_cookie(request) if request is not None else None
    if named is None or named[1] == user.pk:
        return 0
    deleted, _ = PushDevice.objects.filter(pk=named[0]).exclude(membership__user_id=user.pk).delete()
    return deleted


# -- What a browser says it is ----------------------------------------------------------------------------------------


def _platform(agent: str) -> str:
    if "iPhone" in agent or "iPod" in agent:
        return "iPhone"
    if "iPad" in agent:
        return "iPad"
    if "Android" in agent:
        return "Android"
    if "Windows" in agent:
        return "Windows"
    if "CrOS" in agent:
        return "Chromebook"
    if "Macintosh" in agent or "Mac OS X" in agent:
        return "Mac"
    if "Linux" in agent:
        return "Linux"
    return ""


def _browser(agent: str) -> str:
    # Most specific first: every Chromium names Chrome and Safari too.
    for marker, name in (
        ("Edg", "Edge"),
        ("OPR/", "Opera"),
        ("SamsungBrowser", "Samsung Internet"),
        ("Firefox/", "Firefox"),
        ("FxiOS", "Firefox"),
        ("CriOS", "Chrome"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if marker in agent:
            return name
    return ""


def label_from_user_agent(user_agent) -> str:
    """« Android · Chrome », « Windows · Edge », « Mac · Safari »; an iPhone
    or an iPad is « iPhone · application » (iOS pushes to a Home Screen app
    only). At most 80 characters."""
    agent = str(user_agent or "")[:512]
    platform = _platform(agent)
    if platform in {"iPhone", "iPad"}:
        return f"{platform} · application"
    browser = _browser(agent)
    label = " · ".join(part for part in (platform, browser) if part) or DEFAULT_LABEL
    return label[:80]


# -- « Activer » and the sync -----------------------------------------------------------------------------------------


def origin_refusal(request) -> str:
    """« Activer » from another address than SITE_URL's, on a server that
    sends: the notifications' links open SITE_URL, so the device is
    refused there. "" when allowed."""
    if not webpush.sending_enabled():
        return ""
    site = str(settings.SITE_URL or "")
    try:
        site_host = (urlsplit(site).hostname or "").lower()
    except ValueError:
        site_host = ""
    host = request.get_host().rsplit(":", 1)[0].strip("[]").lower()
    if host == site_host:
        return ""
    return f"Activez les notifications depuis {site} : les liens des notifications ouvrent cette adresse."


def _checked(endpoint, p256dh, auth, server_key) -> str:
    """The endpoint rebuilt from its checked parts; DeviceRefused for a
    subscription the server cannot use."""
    try:
        endpoint = webpush.check_endpoint(endpoint)
        webpush.check_keys(p256dh, auth)
    except (webpush.BadEndpoint, webpush.BadKeys) as refusal:
        raise DeviceRefused(str(refusal)) from None
    if not isinstance(server_key, str) or server_key != webpush.vapid_public_key():
        raise DeviceRefused(KEY_CHANGED)
    return endpoint


def _accounts_atomic():
    return transaction.atomic(using=router.db_for_write(PushDevice))


def register(membership: Membership, *, endpoint, p256dh, auth, server_key, user_agent="", now=None) -> PushDevice:
    """« Activer »: this browser's subscription for `membership` - created,
    refreshed, or moved here when its keys are the stored ones. DeviceRefused
    with the page's sentence otherwise."""
    endpoint = _checked(endpoint, p256dh, auth, server_key)
    now = now or timezone.now()
    label = label_from_user_agent(user_agent)
    for attempt in range(2):
        try:
            with _accounts_atomic():
                device = PushDevice.objects.filter(endpoint=endpoint).first()
                if device is not None and (device.p256dh != p256dh or device.auth != auth):
                    raise DeviceRefused(ALREADY_REGISTERED)
                if device is None:
                    device = PushDevice.objects.create(
                        membership=membership,
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        server_key=server_key,
                        label=label,
                        created_at=now,
                        seen_at=now,
                    )
                else:
                    device.membership = membership
                    device.server_key = server_key
                    device.label = label
                    device.seen_at = now
                    device.gone_at = None
                    device.logged_out_at = None
                    device.save(
                        update_fields=["membership", "server_key", "label", "seen_at", "gone_at", "logged_out_at"]
                    )
                _evict(membership, keep=device.pk)
                return device
        except IntegrityError:
            # The same endpoint inserted by another request a moment
            # before: read again, once.
            if attempt:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def _evict(membership: Membership, *, keep: int) -> None:
    extra = list(
        PushDevice.objects.filter(membership=membership)
        .exclude(pk=keep)
        .order_by("-seen_at", "-pk")
        .values_list("pk", flat=True)[MAX_DEVICES - 1 :]
    )
    if extra:
        PushDevice.objects.filter(pk__in=extra).delete()


@dataclass(frozen=True)
class Synced:
    """A sync's answer: `state` (OK, RENEW, UNKNOWN) and the device found
    (None for UNKNOWN), whose cookie the view sets."""

    state: str
    device: PushDevice | None = None


def sync(
    membership: Membership, *, cookie_device_pk=None, endpoint=None, p256dh=None, auth=None, server_key=None, now=None
):
    """The page's sync (see the module's docstring). `cookie_device_pk`: what
    `cookie_device_pk(request)` answers. Never creates; DeviceRefused for a
    subscription that cannot be one (a foreign endpoint, keys that are no
    keys). Synced.device, when found: the view names it in the cookie."""
    now = now or timezone.now()
    device = None
    if cookie_device_pk is not None:
        device = PushDevice.objects.filter(pk=cookie_device_pk, membership=membership).first()
    checked = None
    if endpoint is not None:
        try:
            checked = webpush.check_endpoint(endpoint)
        except webpush.BadEndpoint as refusal:
            raise DeviceRefused(str(refusal)) from None
        if device is None:
            device = PushDevice.objects.filter(endpoint=checked, membership=membership).first()
    if device is None:
        return Synced(UNKNOWN)
    if checked is None:
        return Synced(RENEW, device)
    try:
        webpush.check_keys(p256dh, auth)
    except webpush.BadKeys as refusal:
        raise DeviceRefused(str(refusal)) from None
    if not isinstance(server_key, str) or server_key != webpush.vapid_public_key():
        return Synced(RENEW, device)
    with _accounts_atomic():
        holder = PushDevice.objects.filter(endpoint=checked).first()
        if holder is not None and holder.gone_at is not None:
            return Synced(RENEW, device)
        if holder is not None and holder.pk != device.pk:
            if holder.membership_id != membership.pk:
                # Another login's subscription: a sync never moves a device.
                return Synced(UNKNOWN)
            holder.delete()
        fields = ["endpoint", "p256dh", "auth", "server_key", "seen_at", "gone_at", "logged_out_at"]
        if device.endpoint != checked:
            # A fresh subscription: the failures counted on the old endpoint
            # are not its own (prune would delete a device just recovered).
            device.failures = 0
            device.last_error = ""
            fields += ["failures", "last_error"]
        device.endpoint = checked
        device.p256dh = p256dh
        device.auth = auth
        device.server_key = server_key
        device.seen_at = now
        # A live subscription under the current key (a tombstoned holder
        # answered RENEW above): the device is sent to again - the renewal
        # a RENEW asks for ends here, as « Activer » does in `register`.
        device.gone_at = None
        device.logged_out_at = None
        device.save(update_fields=fields)
    return Synced(OK, device)


def remove(membership: Membership, device_pk) -> bool:
    """« Retirer » / « Désactiver sur cet appareil »: one of this login's
    devices deleted. False when it is none of them."""
    deleted, _ = PushDevice.objects.filter(pk=device_pk, membership=membership).delete()
    return bool(deleted)


def devices_of(membership: Membership) -> list[PushDevice]:
    """« Mes appareils »: this login's devices in this espace, the last seen
    first."""
    return list(PushDevice.objects.filter(membership=membership).order_by("-seen_at", "-pk"))


def needs_renewal(device: PushDevice, key: str | None = None) -> bool:
    """« à réactiver »: tombstoned, logged out, or subscribed under another
    server key - sent nothing until « Activer » or a sync."""
    key = key if key is not None else webpush.vapid_public_key()
    return device.gone_at is not None or device.logged_out_at is not None or device.server_key != key


def prune(tenant_id: int, now=None) -> int:
    """The tick's housekeeping of one espace's devices: tombstones older
    than TOMBSTONE_DAYS, and devices failing FAILING_AFTER times in a row
    with no success for FAILING_DAYS. How many went."""
    now = now or timezone.now()
    devices = PushDevice.objects.filter(membership__tenant_id=tenant_id)
    gone, _ = devices.filter(gone_at__lt=now - timedelta(days=TOMBSTONE_DAYS)).delete()
    quiet_since = now - timedelta(days=FAILING_DAYS)
    # No success since then: none ever (and registered before), or the last
    # one before.
    no_success = Q(last_success_at__lt=quiet_since) | Q(last_success_at__isnull=True, created_at__lt=quiet_since)
    dead, _ = devices.filter(no_success, failures__gte=FAILING_AFTER).delete()
    return gone + dead
