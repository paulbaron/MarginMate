"""Base test cases.

`NoNetworkTestCase` is the belt to `config.settings_test`'s braces: the test
settings blank every credential so the real integrations refuse to start,
and this additionally replaces the libraries that could reach the outside
world with objects that raise on use - and a DNS lookup of any name but the
machine's own. A test that accidentally reaches for the real mailbox or the
real Metro site then fails immediately with a clear message, instead of
hanging on a socket timeout or - far worse - quietly succeeding against real
data.
"""

from __future__ import annotations

import ipaddress
import socket
from unittest import mock

from django.test import TestCase


class _Forbidden:
    """Stands in for a network client class; explodes if anything uses it."""

    def __init__(self, what: str):
        self._what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            f"Test tried to open a real {self._what} connection. Mock it explicitly in the test instead."
        )


#: The real lookup, for the machine's own names.
_GETADDRINFO = socket.getaddrinfo


def _no_lookup(name) -> bool:
    """Whether getaddrinfo answers `name` without asking the network: none
    (a wildcard), an address written as one (a server binding 0.0.0.0, a
    test's 127.0.0.1), or the machine's own name."""
    if not name:
        return True
    text = name.decode() if isinstance(name, bytes) else str(name)
    if text.lower() == "localhost" or text.lower().endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _guarded_getaddrinfo(host, *args, **kwargs):
    """socket.getaddrinfo for the machine's own names only: a name looked up
    for real is a test reaching the network (the mailbox's server checked by
    invoices.scrapers.egress) - patch egress.resolve instead."""
    if _no_lookup(host):
        return _GETADDRINFO(host, *args, **kwargs)
    raise AssertionError(
        f"Test tried to resolve {host!r} for real. Patch invoices.scrapers.egress.resolve in the test instead."
    )


class ForbiddenNetworkCall(BaseException):
    """What the run-wide push stub raises. Not an Exception on purpose: the
    delivery path turns any Exception into a dispatch « erreur interne »
    (notifications.sending.deliver_one), so an AssertionError there let a
    test that forgot to mock pass quietly. unittest records this as the
    test's error."""


class _ForbiddenPush(_Forbidden):
    """The push transport's stand-in for the whole run (tests/runner.py
    install): fails the test through every `except Exception`."""

    def __call__(self, *args, **kwargs):
        raise ForbiddenNetworkCall(
            f"Test tried to open a real {self._what} connection. Mock it explicitly in the test instead."
        )


class NoNetworkTestCase(TestCase):
    """TestCase that makes any real outbound connection fail loudly.

    A test that *does* need one of these (to assert on how it's called)
    should patch it itself - `mock.patch` inside the test wins over the
    class-level patch, so no opt-out flag is needed.
    """

    # (module path, attribute) pairs patched for the duration of each test.
    # Patched where they're looked up at call time (the library module), so
    # this holds regardless of how our own code imports them.
    # Deliberately the specific client classes rather than something broad
    # like socket.create_connection, which the test runner itself and any
    # LiveServerTestCase legitimately need.
    _FORBIDDEN = [
        ("imaplib.IMAP4_SSL", "IMAP"),
        ("smtplib.SMTP", "SMTP"),
        ("smtplib.SMTP_SSL", "SMTP"),
    ]

    def setUp(self):
        super().setUp()
        for target, label in self._FORBIDDEN:
            patcher = mock.patch(target, new=_Forbidden(label))
            patcher.start()
            self.addCleanup(patcher.stop)
        # A DNS lookup is the network too: the machine's own names only.
        patcher = mock.patch("socket.getaddrinfo", new=_guarded_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

        # selenium is optional at runtime; only guard it if it is actually
        # installed, so the suite still runs without it.
        # pyHanko's two HTTP timestamp clients (the timesheet signatures,
        # staff/signing.py) are guarded where they send the request, so an
        # instance made however it was imported still fails loudly: a test
        # signs with DummyTimeStamper (staff.tests.signing_support), never
        # with DigiCert.
        for target, label in (
            ("selenium.webdriver.Chrome", "Selenium/Chrome"),
            (
                "pyhanko.sign.timestamps.requests_client.RequestsHTTPTimeStamper.async_request_tsa_response",
                "timestamp server (RFC 3161)",
            ),
            (
                "pyhanko.sign.timestamps.aiohttp_client.HTTPTimeStamper.async_request_tsa_response",
                "timestamp server (RFC 3161)",
            ),
        ):
            try:
                patcher = mock.patch(target, new=_Forbidden(label))
                patcher.start()
            except (ImportError, AttributeError, ModuleNotFoundError):
                continue
            self.addCleanup(patcher.stop)
