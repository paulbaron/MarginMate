"""Base test cases.

`NoNetworkTestCase` is the belt to `config.settings_test`'s braces: the test
settings blank every credential so the real integrations refuse to start,
and this additionally replaces the three libraries that could reach the
outside world with objects that raise on use. A test that accidentally
reaches for the real mailbox, the real Metro site or the real Anthropic API
then fails immediately with a clear message, instead of hanging on a socket
timeout or - far worse - quietly succeeding against real data.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase


class _Forbidden:
    """Stands in for a network client class; explodes if anything uses it."""

    def __init__(self, what: str):
        self._what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            f"Test tried to open a real {self._what} connection. "
            f"Mock it explicitly in the test instead."
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

        # selenium and anthropic are optional at runtime; only guard them if
        # they're actually installed, so the suite still runs without them.
        for target, label in (
            ("selenium.webdriver.Chrome", "Selenium/Chrome"),
            ("anthropic.Anthropic", "Anthropic API"),
        ):
            try:
                patcher = mock.patch(target, new=_Forbidden(label))
                patcher.start()
            except (ImportError, AttributeError, ModuleNotFoundError):
                continue
            self.addCleanup(patcher.stop)
