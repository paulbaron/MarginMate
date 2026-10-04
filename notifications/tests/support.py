"""What the notifications' tests share: invented subscriptions, a push
service that answers what a test says (never the network: the run's guard,
tests/runner.py, stands behind it), and a production-like server.

Every endpoint, key, address and name here is invented."""

from __future__ import annotations

import itertools
import logging
from contextlib import contextmanager
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from accounts.models import Membership, PushDevice
from notifications import sending, webpush

SITE = "https://bar-des-tests.example.invalid"
#: What makes `sending_enabled()` hold under the test settings (DEBUG off,
#: their key is strong): an https SITE_URL.
PRODUCTION = override_settings(SITE_URL=SITE)

_tokens = itertools.count(1)


def new_keys() -> tuple[str, str]:
    """A browser's subscription keys, made now: (p256dh, auth)."""
    private = ec.generate_private_key(ec.SECP256R1())
    return webpush.b64u_encode(webpush._public_bytes(private)), webpush.b64u_encode(b"auth-des-tests16")


def new_endpoint(host="fcm.googleapis.com") -> str:
    return f"https://{host}/fcm/send/jeton-invente-{next(_tokens):05d}"


def make_device(membership: Membership, *, endpoint=None, server_key=None, **fields) -> PushDevice:
    """A device of `membership`, subscribed under the current key unless
    told otherwise."""
    p256dh, auth = new_keys()
    values = {
        "membership": membership,
        "endpoint": endpoint or new_endpoint(),
        "p256dh": p256dh,
        "auth": auth,
        "server_key": webpush.vapid_public_key() if server_key is None else server_key,
        "label": "Android · Chrome",
    }
    values.update(fields)
    return PushDevice.objects.create(**values)


def make_login(tenant_id, email, *, role=Membership.Role.MEMBER, active=True) -> Membership:
    """A login with a membership of the espace `tenant_id` (central rows
    only: no tenant file is needed)."""
    user = get_user_model().objects.create_user(username=email, email=email, password="mot-de-passe-essai")
    if not active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    return Membership.objects.create(user=user, tenant_id=tenant_id, role=role)


class Answer:
    def __init__(self, status):
        self.status_code = status


class PushService:
    """Stands for the push services: answers each POST with the next status
    of `statuses` (the last one repeated) and keeps what it was sent."""

    def __init__(self, *statuses):
        self.statuses = list(statuses) or [201]
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        status = self.statuses[min(len(self.calls), len(self.statuses)) - 1]
        if isinstance(status, BaseException):
            raise status
        return Answer(status)

    @property
    def urls(self):
        return [url for url, _ in self.calls]


@contextmanager
def push_service(*statuses):
    """`notifications.webpush._post` answering `statuses`, retries without
    the wait."""
    service = PushService(*statuses)
    with mock.patch("notifications.webpush._post", service), mock.patch("notifications.webpush._sleep"):
        yield service


class QuietLogs:
    """Silence the notifications' loggers for a test (the test settings say
    LOGGING = {}: a warning would reach Python's last-resort handler)."""

    LOGGERS = (
        "notifications.webpush",
        "notifications.sending",
        "notifications.reminders",
        "notifications.events",
        "notifications.scheduler",
        "notifications.signals",
        "accounts.pages",
    )

    def setUp(self):
        super().setUp()
        for name in self.LOGGERS:
            logger = logging.getLogger(name)
            self.enterContext(mock.patch.object(logger, "propagate", False))
            handler = logging.NullHandler()
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
        sending._delivery_threads.clear()
        sending._again.clear()
        self.addCleanup(sending._delivery_threads.clear)
        self.addCleanup(sending._again.clear)
        cache.clear()
