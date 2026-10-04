"""`manage.py serve` (accounts/management/commands/serve.py): the production
server's refusals, and Waitress's wiring behind the Cloudflare Tunnel.

Nothing is served here. Waitress's `create_server` is replaced by a fake
whose `run` returns, or built with `_start=False` and called directly as a
WSGI application; every socket is on 127.0.0.1, on a port the system picks
(never 8765 nor 8000, where the owner's servers may run); every tenant is a
temporary one; the child processes read no .env. Addresses from the
documentation ranges (TEST-NET), names invented.
"""

import contextlib
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
import wsgiref.util
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import (
    Command as RunserverCommand,
)
from django.core.cache import cache
from django.core.handlers.wsgi import WSGIHandler, WSGIRequest
from django.core.management import CommandError, call_command
from django.core.management.commands.runserver import Command as CoreRunserverCommand
from django.core.signals import request_finished, request_started
from django.db import close_old_connections, connections
from django.test import SimpleTestCase, TestCase, override_settings
from waitress import create_server
from waitress.adjustments import Adjustments

from accounts import limiter, paths
from accounts.management.commands import serve
from accounts.tests.support import TenancyTestCase
from accounts.tests.test_production_settings import child_environment
from config import wsgi as config_wsgi
from staff.models import SignatureEvent
from staff.signature_views import client as event_client
from staff.tests.test_sign_public import PublicCase

#: What cloudflared forwards: the visitor's address last in X-Forwarded-For
#: (Cloudflare's edge appends the address that reached it; the first entry
#: here is one a visitor wrote himself), https.
VISITOR = "203.0.113.9"
FORGED = "198.51.100.66"
TUNNEL = {"HTTP_X_FORWARDED_FOR": f"{FORGED}, {VISITOR}", "HTTP_X_FORWARDED_PROTO": "https"}
#: Somebody reaching the server some other way than the tunnel.
STRANGER = "192.0.2.77"
#: What Cloudflare's edge adds to every request cloudflared forwards.
CLOUDFLARE = {
    "HTTP_CF_RAY": "8c0ffee0d15ea5e0-CDG",
    "HTTP_CF_CONNECTING_IP": VISITOR,
    "HTTP_CF_VISITOR": '{"scheme":"https"}',
    "HTTP_CDN_LOOP": "cloudflare",
}

#: A server set up to go online (the rest is the test settings').
ONLINE = {
    "ALLOWED_HOSTS": ["gestion.example.com", "localhost", "127.0.0.1"],
    "SESSION_COOKIE_SECURE": True,
    "CSRF_COOKIE_SECURE": True,
    "SECURE_HSTS_SECONDS": 3600,
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def environ(peer: str, method="GET", path="/", body=b"", **extra) -> dict:
    """A WSGI environ as Waitress hands one over, from `peer`."""
    env = {}
    wsgiref.util.setup_testing_defaults(env)
    env.update(
        {
            "REMOTE_ADDR": peer,
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "HTTP_HOST": "testserver",
            "SERVER_NAME": "testserver",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
    )
    env.update(extra)
    return env


def call(app, env) -> tuple[int, list, bytes]:
    answered = {}

    def start_response(status, headers, exc_info=None):
        answered["status"], answered["headers"] = status, headers

    result = app(env, start_response)
    try:
        body = b"".join(result)
    finally:
        close = getattr(result, "close", None)
        if close is not None:
            close()
    return int(answered["status"][:3]), answered["headers"], body


def tunnel_server(application):
    """Waitress as `serve` sets it up, not started: `.application` is what
    a request from the tunnel reaches (Waitress's proxy handling, then
    `application`)."""
    server = create_server(
        application,
        sockets=[serve.listening_socket(0)],
        _start=False,
        _dispatcher=mock.Mock(),
        **serve.waitress_options(),
    )
    return server


def close_server(server):
    server.close()
    server.trigger.close()


class FakeServer:
    """What `create_server` returns in these tests: `run` returns at once
    (or does what `on_run` says)."""

    def __init__(self, on_run=None):
        self.on_run = on_run
        self.ran = self.closed = False
        self.task_dispatcher = mock.Mock()

    def run(self):
        self.ran = True
        if self.on_run is not None:
            self.on_run()

    def close(self):
        self.closed = True


class WaitressWiringTests(SimpleTestCase):
    """The client's address and scheme, set in ONE place: Waitress trusts
    cloudflared (127.0.0.1) for X-Forwarded-For and X-Forwarded-Proto, and
    nobody else for anything (security audit ANON-4, DEPLOY-4)."""

    def setUp(self):
        self.seen = []

        def recording(env, start_response):
            self.seen.append(dict(env))
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        self.server = tunnel_server(recording)
        self.addCleanup(close_server, self.server)

    def test_the_options(self):
        options = serve.waitress_options()
        self.assertEqual(options["threads"], 8)
        self.assertEqual(options["trusted_proxy"], "127.0.0.1")
        self.assertEqual(options["trusted_proxy_count"], 1)
        self.assertEqual(options["trusted_proxy_headers"], {"x-forwarded-for", "x-forwarded-proto"})
        self.assertIs(options["clear_untrusted_proxy_headers"], True)
        self.assertNotIn("host", options)
        self.assertNotIn("port", options)
        adjustments = Adjustments(sockets=[self.server.socket], **options)
        self.assertIs(adjustments.expose_tracebacks, False)
        self.assertEqual(adjustments.ident, "MarginMate")
        # Bound to this machine only.
        self.assertEqual(self.server.socket.getsockname()[0], "127.0.0.1")

    def test_the_biggest_upload_the_app_takes_gets_through(self):
        """Waitress refuses a body over `max_request_body_size` with its own
        English 413 before Django sees it: its default, 1 GB, stopped a
        « Données » archive the page itself takes (4 GB) - and DEPLOY.md
        sends a big archive to http://127.0.0.1:8765, where no Cloudflare
        cap stands in front. Through the tunnel Cloudflare's own cap (100 MB
        on its free plan) is lower than every form's."""
        from invoices.forms import RECEIPT_BATCH_MAX_BYTES
        from transfer.archive import MAX_ARCHIVE_BYTES

        adjustments = Adjustments(sockets=[self.server.socket], **serve.waitress_options())
        # The archive, its multipart framing and the form's other fields.
        self.assertGreater(adjustments.max_request_body_size, MAX_ARCHIVE_BYTES)
        self.assertGreater(adjustments.max_request_body_size, RECEIPT_BATCH_MAX_BYTES)
        # Not unbounded: a body past every cap the app has is still refused.
        self.assertLessEqual(adjustments.max_request_body_size, MAX_ARCHIVE_BYTES + 64 * 1024**2)

    def test_through_the_tunnel_a_request_is_the_visitor_s(self):
        status, _, _ = call(
            self.server.application,
            environ(
                "127.0.0.1",
                HTTP_X_FORWARDED_HOST="ailleurs.example",
                HTTP_X_FORWARDED_PORT="8443",
                HTTP_FORWARDED="for=192.0.2.1;proto=http",
                **TUNNEL,
            ),
        )
        self.assertEqual(status, 200)
        (seen,) = self.seen
        self.assertEqual(seen["REMOTE_ADDR"], VISITOR)
        self.assertEqual(seen["wsgi.url_scheme"], "https")
        # Nothing else a proxy could say is trusted, even from cloudflared.
        for header in ("HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PORT", "HTTP_FORWARDED"):
            self.assertNotIn(header, seen)
        self.assertEqual(seen["HTTP_HOST"], "testserver")
        request = WSGIRequest(seen)  # ty: ignore[too-many-positional-arguments]  # a stubs-only __new__; Django's HttpRequest has none
        self.assertTrue(request.is_secure())
        # What the login limiter counts, and what a signature event records.
        self.assertEqual(limiter.client_ip(request), VISITOR)
        self.assertEqual(event_client(request)["ip"], VISITOR)

    def test_from_anywhere_else_the_forwarded_headers_are_dropped(self):
        call(self.server.application, environ(STRANGER, HTTP_X_FORWARDED_HOST="ailleurs.example", **TUNNEL))
        (seen,) = self.seen
        self.assertEqual(seen["REMOTE_ADDR"], STRANGER)
        self.assertEqual(seen["wsgi.url_scheme"], "http")
        self.assertEqual([name for name in seen if "FORWARDED" in name], [])
        request = WSGIRequest(seen)  # ty: ignore[too-many-positional-arguments]  # a stubs-only __new__; Django's HttpRequest has none
        self.assertFalse(request.is_secure())
        self.assertEqual(limiter.client_ip(request), STRANGER)

    def test_the_port_is_the_server_s_alone(self):
        """Windows lets a second server bind a port already bound with
        SO_REUSEADDR (runserver's way): a forgotten runserver would have
        shared the tunnel's requests. The server's socket is exclusive."""
        port = self.server.socket.getsockname()[1]
        with self.assertRaises(OSError):
            serve.listening_socket(port)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: a runserver's socket, re-usable, is refused the port.
            second = socket.socket()
            self.addCleanup(second.close)
            second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                second.bind(("127.0.0.1", port))


class TunnelGuardTests(SimpleTestCase):
    """PROD-1's second lock: config.wsgi - what runserver serves, and any
    WSGI server loading it - refuses a request that came through the
    Cloudflare Tunnel. Under DEBUG a runserver on the tunnel's port answered
    the public Host it did not list with Django's technical page: the
    settings, request.META with the .env's addresses."""

    def setUp(self):
        self.reached = []

        def inner(env, start_response):
            self.reached.append(env)
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        self.guarded = config_wsgi.refuse_the_tunnel(inner)

    def refused(self, application, env) -> tuple[int, list, bytes]:
        """`application` asked `env` as the first refusal of its process:
        the refusal is said in the console."""
        with mock.patch.object(config_wsgi, "_said", False), self.assertLogs("config.wsgi", "WARNING"):
            return call(application, env)

    def test_every_header_cloudflare_adds_is_a_refusal(self):
        for name, value in CLOUDFLARE.items():
            with self.subTest(header=name):
                status, headers, body = self.refused(self.guarded, environ("127.0.0.1", **{name: value}))
                self.assertEqual(status, 503)
                self.assertEqual(body.decode("utf-8"), config_wsgi.REFUSAL)
                self.assertIn(("Cache-Control", "no-store"), headers)
        self.assertEqual(self.reached, [])

    def test_it_is_said_once_in_the_console(self):
        with mock.patch.object(config_wsgi, "_said", False), self.assertLogs("config.wsgi", "WARNING") as said:
            for _ in range(3):
                call(self.guarded, environ("127.0.0.1", **CLOUDFLARE))
        (line,) = said.output
        self.assertIn("tunnel Cloudflare", line)
        self.assertIn("http://127.0.0.1:8765", line)
        self.assertIn("start_production.cmd", line)

    def test_everything_else_goes_through(self):
        status, _, body = call(self.guarded, environ(STRANGER, **TUNNEL))
        self.assertEqual((status, body), (200, b"ok"))
        status, _, _ = call(self.guarded, environ("127.0.0.1"))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.reached), 2)


class RunserverOnTheTunnelTests(SimpleTestCase):
    def test_runserver_refuses_the_tunnel_with_or_without_debug(self):
        """The handler Django's runserver really builds (the staticfiles
        one: DEBUG adds its static handler), asked for a page with the
        public Host it does not list - the setup window DEPLOY.md used to
        leave open, where it answered 400 with Django's technical page."""
        for debug in (True, False):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                handler = RunserverCommand().get_handler(use_static_handler=True, insecure_serving=False)
                env = environ("127.0.0.1", path="/connexion/", HTTP_HOST="gestion.example.com", **TUNNEL, **CLOUDFLARE)
                with (
                    mock.patch.object(config_wsgi, "_said", False, create=True),
                    self.assertLogs("config.wsgi", "WARNING"),
                ):
                    status, _, body = call(handler, env)
                    for leak in (b"DisallowedHost", b"Traceback", b"TENANTS_ROOT", b"SECRET"):
                        self.assertNotIn(leak, body)
                    self.assertEqual(status, 503)
                    self.assertEqual(body.decode("utf-8"), config_wsgi.REFUSAL)


class ThroughTheTunnelTests(TestCase):
    """The whole application behind Waitress's proxy handling: the login
    limiter counts the visitor's address, and an https request gets HSTS."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # As Django's test client does: a request's end must not close the
        # test's connections (its transaction).
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        self.addCleanup(request_started.connect, close_old_connections)
        self.addCleanup(request_finished.connect, close_old_connections)
        with override_settings(SECURE_HSTS_SECONDS=3600):
            # The middleware reads its settings when it is built.
            self.server = tunnel_server(WSGIHandler())
        self.addCleanup(close_server, self.server)

    def log_in(self, peer, email, origin="https://testserver"):
        """A wrong password for `email`, POSTed as a browser does (the
        page's CSRF cookie and token, its Origin), from `peer` with the
        tunnel's headers."""
        status, headers, _ = call(self.server.application, environ(peer, path="/connexion/", **TUNNEL))
        self.assertEqual(status, 200)
        cookie = next(
            value.strip() for name, value in headers if name == "Set-Cookie" and value.strip().startswith("csrftoken=")
        )
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        body = urlencode({"username": email, "password": "pas-le-bon", "csrfmiddlewaretoken": token}).encode()
        status, headers, page = call(
            self.server.application,
            environ(
                peer,
                method="POST",
                path="/connexion/",
                body=body,
                CONTENT_TYPE="application/x-www-form-urlencoded",
                HTTP_COOKIE=f"csrftoken={token}",
                HTTP_ORIGIN=origin,
                **TUNNEL,
            ),
        )
        return status, dict(headers), page

    def counted(self, ip, email):
        pair = limiter._key(limiter.LOGIN, "pair", f"{ip}\n{email}")
        return cache.get(pair)

    def test_the_limiter_counts_the_visitor_not_the_tunnel(self):
        status, headers, page = self.log_in("127.0.0.1", "alpha@example.invalid")
        self.assertEqual(status, 200)
        self.assertIn("Adresse e-mail ou mot de passe incorrect.", page.decode())
        self.assertEqual(self.counted(VISITOR, "alpha@example.invalid"), 1)
        self.assertIsNone(self.counted("127.0.0.1", "alpha@example.invalid"))
        self.assertIsNone(self.counted(FORGED, "alpha@example.invalid"))
        # Over https (as Cloudflare said): the browser is told to stay there.
        self.assertEqual(headers.get("Strict-Transport-Security"), "max-age=3600")

    def test_a_stranger_s_forwarded_headers_count_for_nothing(self):
        # Plain http from his own address, whatever he says: a page of
        # https://testserver is another origin.
        status, headers, _ = self.log_in(STRANGER, "alpha@example.invalid")
        self.assertEqual(status, 403)
        self.assertNotIn("Strict-Transport-Security", headers)
        status, headers, _ = self.log_in(STRANGER, "alpha@example.invalid", origin="http://testserver")
        self.assertEqual(status, 200)
        self.assertNotIn("Strict-Transport-Security", headers)
        self.assertEqual(self.counted(STRANGER, "alpha@example.invalid"), 1)
        self.assertIsNone(self.counted(VISITOR, "alpha@example.invalid"))


class SigningEventThroughTheTunnelTests(PublicCase):
    """The employee's signing link opened through the tunnel: the event in
    the proof file records his address, never 127.0.0.1."""

    def test_the_link_opened_records_the_visitor_s_address(self):
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        self.addCleanup(request_started.connect, close_old_connections)
        self.addCleanup(request_finished.connect, close_old_connections)
        server = tunnel_server(WSGIHandler())
        self.addCleanup(close_server, server)
        status, _, _ = call(server.application, environ("127.0.0.1", path=self.url, **TUNNEL))
        self.assertEqual(status, 200)
        opened = self.request.events.filter(kind=SignatureEvent.Kind.LINK_OPENED)
        self.assertEqual(list(opened.values_list("ip", flat=True)), [VISITOR])


@override_settings(**ONLINE)
class ServeTests(TenancyTestCase):
    """The refusals, in a real installation: the accounts database, the
    template, an open tenant - temporary ones."""

    def setUp(self):
        super().setUp()
        self.tenant = self.make_tenant("Bar Essai")
        self.static_root = Path(tempfile.mkdtemp(prefix="marginmate-tests-static-"))
        self.enterContext(override_settings(STATIC_ROOT=self.static_root))
        #: Stands for notifications.scheduler.start in every run_serve.
        self.scheduler = mock.Mock(name="notifications.scheduler.start")

    def run_serve(self, *args, server=None, collect=False):
        """`manage.py serve *args`, with Waitress faked: (stdout, stderr,
        the create_server calls, the fake server)."""
        server = server or FakeServer()
        calls = []

        def fake_create_server(application, **options):
            (sock,) = options["sockets"]
            calls.append(
                {
                    "application": application,
                    "options": options,
                    "address": sock.getsockname(),
                    "debug": settings.DEBUG,
                    "socket": sock,
                    "finders": settings.WHITENOISE_USE_FINDERS,
                    "autorefresh": settings.WHITENOISE_AUTOREFRESH,
                }
            )
            return server

        out, err = io.StringIO(), io.StringIO()
        self.out, self.err, self.calls, self.server = out, err, calls, server
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("waitress.create_server", fake_create_server))
            # Never a real scheduler thread here: what it would start, and
            # whether it was stopped (`self.scheduler`, made in setUp).
            stack.enter_context(mock.patch("notifications.scheduler.start", self.scheduler))
            # collectstatic, unless the test wants it run for real.
            self.collected = None if collect else stack.enter_context(mock.patch.object(serve, "call_command"))
            call_command("serve", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), calls, server

    def refused(self, *args) -> str:
        with self.assertRaises(CommandError) as refusal:
            self.run_serve("--verifier", *args)
        self.assertIn("Le serveur ne démarre pas", str(refusal.exception))
        self.assertEqual(self.calls, [])
        return self.err.getvalue()

    # -- Serving ------------------------------------------------------------------------------------------------

    def test_everything_in_order_it_serves_on_127_0_0_1_with_waitress(self):
        port = free_port()
        out, err, calls, server = self.run_serve("--port", str(port), collect=True)
        self.assertEqual(err, "")
        (served,) = calls
        self.assertIsInstance(served["application"], WSGIHandler)
        self.assertEqual(served["address"], ("127.0.0.1", port))
        options = dict(served["options"])
        options.pop("sockets")
        self.assertEqual(options, serve.waitress_options())
        self.assertIs(served["debug"], False)
        self.assertTrue(server.ran)
        self.assertTrue(server.closed)
        # The socket is given back when the server stops.
        self.assertEqual(served["socket"].fileno(), -1)
        # collectstatic ran: WhiteNoise serves STATIC_ROOT.
        self.assertTrue((self.static_root / "css" / "marginmate.css").is_file())
        self.assertIn("Vérifications : tout est en ordre.", out)
        self.assertLess(
            out.index("Rappels et récupérations automatiques : actifs."),
            out.index(f"En ligne sur http://127.0.0.1:{port}/"),
        )
        self.assertIn("Ctrl+C pour arrêter.", out)
        self.assertTrue(out.rstrip().endswith("Serveur arrêté."))
        # Another port than the tunnel's is not called the tunnel's address.
        self.assertNotIn("c'est l'adresse du tunnel Cloudflare", out)
        self.assertIn(f"le tunnel Cloudflare vise le port {serve.DEFAULT_PORT}", out)

    def test_the_scheduler_runs_while_it_serves_and_stops_with_it(self):
        """Started once the socket is the server's and before it runs, said
        before « En ligne », stopped on the way out - Ctrl+C included."""
        order = []
        server = FakeServer(on_run=lambda: order.append("run"))
        self.scheduler.side_effect = lambda: order.append("start") or self.scheduler.return_value
        self.scheduler.return_value.stop.side_effect = lambda **kw: order.append(("stop", kw))
        out, err, _, _ = self.run_serve("--port", str(free_port()), server=server)
        self.assertEqual(err, "")
        self.assertEqual(order, ["start", "run", ("stop", {"timeout": 5})])
        self.scheduler.assert_called_once_with()
        lines = out.splitlines()
        said = lines.index("Rappels et récupérations automatiques : actifs.")
        online = next(n for n, line in enumerate(lines) if line.startswith("En ligne sur"))
        self.assertLess(said, online)
        self.assertTrue(out.rstrip().endswith("Serveur arrêté."))

        def interrupted():
            raise KeyboardInterrupt

        self.scheduler = mock.Mock(name="notifications.scheduler.start")
        self.run_serve("--port", str(free_port()), server=FakeServer(on_run=interrupted))
        self.scheduler.assert_called_once_with()
        self.scheduler.return_value.stop.assert_called_once_with(timeout=5)

    def test_the_scheduler_never_starts_for_the_checks_nor_a_port_refused(self):
        self.run_serve("--verifier")
        self.scheduler.assert_not_called()
        other = socket.socket()
        self.addCleanup(other.close)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        with self.assertRaises(CommandError):
            self.run_serve("--port", str(other.getsockname()[1]))
        self.scheduler.assert_not_called()

    def test_on_the_tunnel_s_port_it_says_so(self):
        port = free_port()
        with mock.patch.object(serve, "DEFAULT_PORT", port):
            out, _, _, _ = self.run_serve("--port", str(port))
        self.assertIn(f"En ligne sur http://127.0.0.1:{port}/ (8 fils) : c'est l'adresse du tunnel Cloudflare.", out)

    def test_check_only_checks_and_stops_there(self):
        out, _, calls, _ = self.run_serve("--verifier")
        self.assertIn("Vérifications : tout est en ordre.", out)
        self.assertEqual(calls, [])
        self.collected.assert_not_called()

    def test_ctrl_c_stops_it_cleanly(self):
        def interrupted():
            raise KeyboardInterrupt

        out, _, _, server = self.run_serve("--port", str(free_port()), server=FakeServer(on_run=interrupted))
        server.task_dispatcher.shutdown.assert_called_once()
        self.assertTrue(server.closed)
        self.assertTrue(out.rstrip().endswith("Serveur arrêté."))

    def test_a_port_in_use_is_refused(self):
        """A runserver still on the port (its socket re-usable, as on
        Windows) is not shared: `serve` refuses."""
        other = socket.socket()
        self.addCleanup(other.close)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        with self.assertRaises(CommandError) as refusal:
            self.run_serve("--port", str(port))
        self.assertIn(f"Le port {port} est déjà pris", str(refusal.exception))
        self.assertEqual(self.calls, [])
        # Refused before anything is done.
        self.collected.assert_not_called()

    def test_the_port_is_a_port(self):
        for port in ("0", "70000"):
            with self.subTest(port=port), self.assertRaises(CommandError):
                self.run_serve("--port", port)

    # -- The tunnel's port, the static files ---------------------------------------------------------------

    def test_its_port_is_8765_never_runserver_s(self):
        """PROD-1: the tunnel pointed at 8000 - runserver's default, and both
        .claude/launch.json previews' - so whichever development server held
        it was the public site."""
        options = serve.Command().create_parser("manage.py", "serve").parse_args([])
        self.assertEqual(options.port, 8765)
        self.assertEqual(serve.DEFAULT_PORT, 8765)
        self.assertNotEqual(str(serve.DEFAULT_PORT), CoreRunserverCommand.default_port)
        self.assertNotEqual(str(serve.DEFAULT_PORT), RunserverCommand.default_port)
        self.assertIn("http://127.0.0.1:8765", serve.Command.help)
        self.assertIn("http://127.0.0.1:8765", serve.__doc__)
        self.assertNotIn("127.0.0.1:8000", serve.Command.help + serve.__doc__ + serve.max_body_bytes.__doc__)

    def test_a_tunnel_request_reaches_its_own_handler(self):
        """serve builds its own handler: config.wsgi's refuses the tunnel's
        requests, which are serve's alone."""
        _, _, calls, _ = self.run_serve("--port", str(free_port()))
        application = calls[0]["application"]
        self.assertIs(type(application), WSGIHandler)
        self.assertIsNot(application, config_wsgi.application)
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        self.addCleanup(request_started.connect, close_old_connections)
        self.addCleanup(request_finished.connect, close_old_connections)
        status, _, body = call(
            application, environ(VISITOR, path="/connexion/", HTTP_HOST="gestion.example.com", **CLOUDFLARE)
        )
        self.assertEqual(status, 200)
        self.assertNotIn(config_wsgi.REFUSAL.encode(), body)

    def test_whitenoise_serves_static_root_alone(self):
        """PROD-4: the settings let runserver read the source folders; serve
        reads what it collected, indexed once - whatever the settings say
        (the test settings say the opposite)."""
        self.assertIs(settings.WHITENOISE_USE_FINDERS, True)
        _, _, calls, _ = self.run_serve("--port", str(free_port()))
        self.assertIs(calls[0]["finders"], False)
        self.assertIs(calls[0]["autorefresh"], False)

    def test_the_static_files_are_the_code_s_whatever_their_dates(self):
        """PROD-3: collectstatic skips a source file older than its collected
        copy. A release whose files keep older dates - a zip, a copy that
        keeps them, a backup put back - left the previous scripts in
        STATIC_ROOT, served by WhiteNoise beside the new templates."""
        source = Path(tempfile.mkdtemp(prefix="marginmate-tests-static-source-"))
        (source / "js").mkdir()
        script = source / "js" / "ui.js"
        script.write_text("// version 1\n", encoding="utf-8")
        dropped = source / "js" / "ancien.js"
        dropped.write_text("// retiré du code\n", encoding="utf-8")
        with override_settings(
            STATICFILES_DIRS=[source], STATICFILES_FINDERS=["django.contrib.staticfiles.finders.FileSystemFinder"]
        ):
            self.run_serve("--port", str(free_port()), collect=True)
            self.assertEqual((self.static_root / "js" / "ui.js").read_text(encoding="utf-8"), "// version 1\n")
            # The next release: dated a month back, one file gone from it.
            script.write_text("// version 2\n", encoding="utf-8")
            a_month_ago = time.time() - 30 * 24 * 3600
            os.utime(script, (a_month_ago, a_month_ago))
            dropped.unlink()
            self.run_serve("--port", str(free_port()), collect=True)
        self.assertEqual((self.static_root / "js" / "ui.js").read_text(encoding="utf-8"), "// version 2\n")
        self.assertFalse((self.static_root / "js" / "ancien.js").exists())

    def test_the_tenant_command_refuses_it(self):
        with self.assertRaises(CommandError) as refusal:
            call_command("tenant", self.tenant.dir_name, "serve")
        self.assertIn("ne se lance pas pour un espace", str(refusal.exception))

    # -- DEBUG, the key, the hosts, HTTPS ------------------------------------------------------------------

    @override_settings(DEBUG=True)
    def test_debug_is_forced_off_whatever_the_settings_say(self):
        out, _, calls, _ = self.run_serve("--port", str(free_port()))
        self.assertIs(calls[0]["debug"], False)
        self.assertIs(settings.DEBUG, False)
        self.assertIn("Mode debug : demandé, et COUPÉ", out)

    def test_a_weak_key_is_refused_and_never_printed(self):
        for key in (
            "django-insecure-dev-key-change-me",
            "change-me-to-a-random-secret",
            "django-insecure-" + secrets.token_urlsafe(50),
            "mot-de-passe-du-bar-2026",
            "ab" * 40,
        ):
            with self.subTest(key=key[:12]), override_settings(SECRET_KEY=key):
                said = self.refused()
                self.assertIn("[accounts.E006]", said)
                self.assertIn("get_random_secret_key", said)
                self.assertNotIn(key, said + self.out.getvalue())

    def test_the_public_host_must_be_allowed(self):
        with override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1"]):
            self.assertIn("[accounts.E008] DJANGO_ALLOWED_HOSTS ne nomme aucun hôte public", self.refused())
        with override_settings(ALLOWED_HOSTS=["*"]):
            self.assertIn("[accounts.E008] DJANGO_ALLOWED_HOSTS contient « * »", self.refused())

    def test_the_cookies_must_be_https_only(self):
        with override_settings(SESSION_COOKIE_SECURE=False):
            said = self.refused()
        self.assertIn("[accounts.E009]", said)
        self.assertIn("MARGINMATE_HTTPS=1", said)
        with override_settings(CSRF_COOKIE_SECURE=False):
            self.assertIn("[accounts.E009]", self.refused())

    def test_django_s_own_deployment_warnings_refuse_too(self):
        with override_settings(SECURE_HSTS_SECONDS=0):
            self.assertIn("[security.W004]", self.refused())

    def test_the_signing_links_address(self):
        for url in ("http://gestion.example.com", "https://ailleurs.example.com", "gestion.example.com"):
            with self.subTest(url=url), override_settings(SITE_URL=url):
                self.assertIn("[accounts.E010]", self.refused())
        with override_settings(SITE_URL="https://gestion.example.com"):
            self.assertIn("tout est en ordre", self.run_serve("--verifier")[0])

    # -- The migrations --------------------------------------------------------------------------------------

    def forget_last_migration(self, database: Path, app="returnables") -> str:
        """Make `database` one migration behind: its last `app` migration
        un-recorded (the schema stays)."""
        with sqlite3.connect(database) as con:
            (name,) = con.execute(
                "SELECT name FROM django_migrations WHERE app = ? ORDER BY id DESC LIMIT 1", [app]
            ).fetchone()
            con.execute("DELETE FROM django_migrations WHERE app = ? AND name = ?", [app, name])
        con.close()
        return f"{app}.{name}"

    def recorded(self, database: Path, migration: str) -> bool:
        app, name = migration.split(".", 1)
        con = sqlite3.connect(database)
        try:
            return bool(
                con.execute("SELECT 1 FROM django_migrations WHERE app = ? AND name = ?", [app, name]).fetchall()
            )
        finally:
            con.close()

    def test_a_tenant_behind_is_named_and_never_migrated(self):
        database = paths.tenant_database(self.tenant)
        migration = self.forget_last_migration(database)
        said = self.refused()
        self.assertIn(
            f"L'espace « Bar Essai » ({self.tenant.dir_name}) : 1 migration(s) à appliquer ({migration})", said
        )
        self.assertIn("Sauvegardez d'abord", said)
        self.assertIn("manage.py migrate_tenants", said)
        # Never migrated by the server.
        self.assertFalse(self.recorded(database, migration))

    def test_the_template_behind_is_named(self):
        migration = self.forget_last_migration(paths.template_database())
        said = self.refused()
        self.assertIn(f"Le modèle des nouveaux espaces : 1 migration(s) à appliquer ({migration})", said)

    def test_the_accounts_database_behind_is_named(self):
        with connections["accounts"].cursor() as cursor:
            cursor.execute(
                "SELECT app, name, applied FROM django_migrations WHERE app = 'accounts' ORDER BY id DESC LIMIT 1"
            )
            app, name, applied = cursor.fetchone()
            cursor.execute("DELETE FROM django_migrations WHERE app = %s AND name = %s", [app, name])

        def put_back():
            with connections["accounts"].cursor() as cursor:
                cursor.execute(
                    "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)", [app, name, applied]
                )

        self.addCleanup(put_back)
        self.assertIn(f"La base des comptes : 1 migration(s) à appliquer (accounts.{name})", self.refused())

    def test_a_missing_tenant_database_is_named_and_not_made(self):
        database = paths.tenant_database(self.tenant)
        for leftover in database.parent.glob(database.name + "*"):
            leftover.unlink()
        self.assertIn(f"L'espace « Bar Essai » ({self.tenant.dir_name}) : sa base est introuvable", self.refused())
        self.assertFalse(database.exists())

    def test_a_closed_tenant_is_not_looked_at(self):
        database = paths.tenant_database(self.tenant)
        for leftover in database.parent.glob(database.name + "*"):
            leftover.unlink()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(is_active=False)
        self.assertIn("tout est en ordre", self.run_serve("--verifier")[0])

    def test_a_missing_accounts_database_is_named_and_not_made(self):
        missing = Path(tempfile.mkdtemp(prefix="marginmate-tests-serve-")) / "comptes.sqlite3"
        databases = {**settings.DATABASES, "accounts": {**settings.DATABASES["accounts"], "NAME": str(missing)}}
        with warnings.catch_warnings():
            # Overriding DATABASES warns: only the checks read it here.
            warnings.simplefilter("ignore")
            with override_settings(DATABASES=databases):
                said = self.refused()
        self.assertIn("La base des comptes est introuvable (MARGINMATE_ACCOUNTS_DB).", said)
        self.assertFalse(missing.exists())


def project_file(name: str) -> str:
    return (Path(settings.BASE_DIR) / name).read_text(encoding="utf-8")


def code_blocks(markdown: str) -> list[str]:
    return markdown.split("```")[1::2]


class DeploymentFilesTests(SimpleTestCase):
    #: The tunnel's service URL and serve's address, as DEPLOY.md gives them.
    SERVE = f"127.0.0.1:{serve.DEFAULT_PORT}"

    def test_the_tunnel_is_pointed_at_serve_s_port_everywhere(self):
        """PROD-1: DEPLOY.md, start_production.cmd, the settings' comments
        and CLAUDE.md's production section all named 127.0.0.1:8000 -
        runserver's address - as the tunnel's."""
        deploy = project_file("DEPLOY.md")
        self.assertNotIn("127.0.0.1:8000", deploy)
        self.assertIn(f"URL **`{self.SERVE}`**", deploy)
        self.assertIn(f"En ligne sur http://{self.SERVE}/", deploy)
        self.assertIn(f"l'URL du tunnel est bien `http://{self.SERVE}`", deploy)
        self.assertIn(f"`Le port {serve.DEFAULT_PORT} est déjà pris`", deploy)
        script = project_file("start_production.cmd")
        self.assertIn(f"http://{self.SERVE},", script)
        self.assertNotIn("127.0.0.1:8000", script)
        self.assertNotIn("127.0.0.1:8000", project_file("config/settings.py"))
        claude = project_file("CLAUDE.md")
        production = claude.split("## Going online / production", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"to `http://{self.SERVE}`, where\n`manage.py serve` runs", production)
        self.assertNotIn("127.0.0.1:8000", production)
        # The previews keep runserver's port: never the tunnel's.
        for launch in (
            Path(settings.BASE_DIR) / ".claude" / "launch.json",
            Path(settings.BASE_DIR).parent / ".claude" / "launch.json",
        ):
            if launch.is_file():
                with self.subTest(launch=str(launch)):
                    for configuration in json.loads(launch.read_text(encoding="utf-8"))["configurations"]:
                        self.assertNotEqual(configuration.get("port"), serve.DEFAULT_PORT)
                        self.assertNotIn(str(serve.DEFAULT_PORT), " ".join(configuration.get("runtimeArgs", [])))

    def test_the_public_hostname_is_added_once_serve_is_online(self):
        """PROD-1: DEPLOY.md published gestion.<domaine> at section 3, before
        the .env said DJANGO_DEBUG=False and before « Arrêtez runserver »:
        in between the owner's runserver was the public site."""
        deploy = project_file("DEPLOY.md")
        self.assertEqual(deploy.count("Service : type **HTTP**"), 1)
        published = deploy.index("Service : type **HTTP**")
        for before in (
            "## 5. Le fichier .env",
            "DJANGO_DEBUG=False\nDJANGO_ALLOWED_HOSTS=gestion.<votre-domaine>",
            "**Arrêtez `runserver`**",
            "manage.py serve --verifier",
            "« Vérifications : tout est en ordre. »",
            f"`En ligne sur http://{self.SERVE}/`",
        ):
            with self.subTest(before=before):
                self.assertLess(deploy.index(before), published)
        # The tunnel's own section says not to, and why.
        tunnel = deploy.split("## 3. Le tunnel", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**N'ajoutez pas encore de nom d'hôte public**", tunnel)
        self.assertNotIn("Service : type", tunnel)
        # A hostname added by the old order is taken down first.
        before_starting = deploy.split("## Avant de commencer", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**supprimez-le dès maintenant**", before_starting)

    def test_the_debug_recipe_runs_on_a_copy_of_the_data(self):
        """PROD-2: DEPLOY.md's `runserver 8001` ran on the live data beside
        serve, and any runserver starting there marks serve's running
        gathers failed (invoices/apps.py's startup reaper). Since the two
        copies (30/09/2026), trying something is the development folder's
        job, on its data-dev: every runserver block of DEPLOY.md starts in
        that folder (section 10), whose .env points at the copy, and names
        no production folder nor serve's port."""
        deploy = project_file("DEPLOY.md")
        tenth = deploy.split("## 10. ", 1)[1].split("\n## ", 1)[0]
        recipes = [block for block in code_blocks(deploy) if "runserver" in block]
        self.assertTrue(recipes)
        for block in recipes:
            with self.subTest(block=block):
                self.assertIn(block, tenth)
                first = block.strip().splitlines()[0]
                self.assertEqual(first, 'cd /d "C:\\Users\\<vous>\\Desktop\\Bar application gestion\\AdminMate"')
                self.assertNotIn("C:\\MarginMate", block)
                self.assertNotIn(f" {serve.DEFAULT_PORT}", block)
        development_env = next(block for block in code_blocks(tenth) if "DJANGO_DEBUG=True" in block)
        # Forward slashes: between quotes python-dotenv reads « \t » as a TAB
        # (test_deployment_scripts.DeployDocumentTests pins why).
        self.assertIn("data-dev/tenants", development_env)
        self.assertIn("data-dev/accounts.sqlite3", development_env)
        self.assertIn("**jamais sur les données du site**", deploy)

    def test_the_log_section_says_who_writes_the_file(self):
        """PROD-5: only serve writes marginmate.log."""
        deploy = project_file("DEPLOY.md")
        section = deploy.split("## 9. Le journal", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**Le serveur de production est le seul à écrire ce fichier.**", section)
        self.assertIn("dans leur propre fenêtre seulement", section)

    def test_the_known_limits_say_what_a_logout_forgets(self):
        """A logout clears the drafts only; the preferences stay."""
        limits = project_file("DEPLOY.md").split("## Limites connues", 1)[1]
        self.assertIn("**La déconnexion efface les brouillons.**", limits)
        self.assertIn("Vos préférences restent", limits)
        self.assertIn("les sources cochées", limits)
        self.assertNotIn("La déconnexion vide le navigateur", limits)

    def test_the_known_limits_say_who_too_many_attempts_hold_back(self):
        """LIMITER-LOCKOUT: a stranger guessing at the owner's address never
        holds back a browser the owner logged in on, nor the PC itself."""
        limits = " ".join(project_file("DEPLOY.md").split("## Limites connues", 1)[1].split())
        self.assertIn("**Trop de tentatives.**", limits)
        self.assertIn("ne bloque pas le navigateur où vous vous êtes déjà connecté", limits)
        self.assertIn(f"ni le PC du bar (`http://127.0.0.1:{serve.DEFAULT_PORT}`)", limits)

    def test_no_runserver_is_said_to_fail_the_production_server_s_gathers(self):
        """PROD-2's code half: a runserver starting beside serve reaps only
        the gathers nobody has heard from since it started (invoices/apps.py)
        - the doc no longer gives that as the reason, which is no more."""
        deploy = project_file("DEPLOY.md")
        self.assertNotIn("marquerait « échouées »", deploy)
        self.assertIn("**jamais sur les données du site**", deploy)

    def test_start_production_runs_serve_with_the_project_s_python(self):
        script = (Path(settings.BASE_DIR) / "start_production.cmd").read_bytes()
        # cmd.exe reads it in the console's code page: plain ASCII only, and
        # CRLF (with LF alone a goto can miss its label; .gitattributes).
        self.assertTrue(script.isascii())
        self.assertEqual(script.count(b"\n"), script.count(b"\r\n"))
        text = script.decode("ascii")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn('".venv\\Scripts\\python.exe" manage.py serve %*', text)
        # DEBUG is serve's to force off, never the script's to turn on.
        self.assertNotIn("DJANGO_DEBUG", text)
        # Serving needs .venv alone: the scheduled task runs it, uv or not.
        commands = [
            line.strip() for line in text.splitlines() if line.strip() and not line.strip().lower().startswith("rem")
        ]
        self.assertEqual([line for line in commands if " uv " in f" {line} "], [])

    def test_the_server_s_packages_are_pinned_at_what_is_installed(self):
        import tomllib
        from importlib.metadata import version

        lock = tomllib.loads((Path(settings.BASE_DIR) / "uv.lock").read_text(encoding="utf-8"))
        pins = {package["name"]: package["version"] for package in lock["package"]}
        for package in ("waitress", "whitenoise"):
            with self.subTest(package=package):
                self.assertEqual(pins[package], version(package))
        self.assertEqual(pins["waitress"], "3.0.2")
        self.assertEqual(pins["whitenoise"], "6.12.0")


#: Run in a child process from the project's folder, reading no .env:
#: manage.py's main() up to the point where Django takes over, which prints
#: DEBUG as the settings decided it, where WhiteNoise reads /static/ from
#: (the middleware as the settings build it) and whether this process
#: writes the log file.
MANAGE = r"""
import sys
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import django.core.management as management


def report(argv):
    import logging
    import warnings
    import django
    django.setup()
    from django.conf import settings
    from whitenoise.middleware import WhiteNoiseMiddleware
    print("DEBUG=" + str(settings.DEBUG))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # « No directory at » STATIC_ROOT: a checkout never served
        static = WhiteNoiseMiddleware(lambda request: None)
    print("FINDERS=" + str(static.use_finders))
    print("AUTOREFRESH=" + str(static.autorefresh))
    print("LOG_FILE=" + str(any(isinstance(handler, logging.FileHandler) for handler in logging.getLogger().handlers)))


management.execute_from_command_line = report
sys.argv = ["manage.py"] + sys.argv[1:]
import manage
manage.main()
"""


class ManagePyTests(SimpleTestCase):
    """manage.py turns DEBUG off for `serve` BEFORE the settings load: the
    .env's DJANGO_DEBUG=True changes nothing, and a weak key is refused at
    load like on any server with DEBUG off."""

    def run_manage(self, *args, **environment):
        folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-manage-"))
        env = child_environment(
            **{
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
                "MARGINMATE_TENANTS_ROOT": str(folder / "espaces"),
                "MARGINMATE_ACCOUNTS_DB": str(folder / "comptes.sqlite3"),
                "MARGINMATE_LOG_DIR": str(folder / "journal"),
                "PYTHONIOENCODING": "utf-8",
                **environment,
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", MANAGE, *args],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [], "loading the settings made nothing")
        return result

    def reported(self, *args, **environment) -> dict:
        result = self.run_manage(*args, **environment)
        lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertIn("DEBUG", lines, result.stderr[-3000:])
        return lines

    def test_serve_runs_without_debug_whatever_the_environment_says(self):
        self.assertIn("DEBUG=False", self.run_manage("serve", "--verifier", DJANGO_DEBUG="True").stdout)

    def test_another_command_keeps_what_the_environment_says(self):
        self.assertIn("DEBUG=True", self.run_manage("check", DJANGO_DEBUG="True").stdout)

    def test_runserver_reads_the_source_static_files_whatever_debug_says(self):
        """PROD-4: with the deployed .env (DEBUG off) runserver served the
        copy the last `serve` collected - an edited script came back as its
        old content under a new ?v=, a new one was a 404."""
        for debug in ("False", "True"):
            with self.subTest(debug=debug):
                lines = self.reported("runserver", "127.0.0.1:8000", DJANGO_DEBUG=debug)
                self.assertEqual((lines["FINDERS"], lines["AUTOREFRESH"]), ("True", "True"))
        # serve: STATIC_ROOT alone, indexed once, whatever .env says.
        lines = self.reported("serve", "--verifier", DJANGO_DEBUG="True")
        self.assertEqual((lines["FINDERS"], lines["AUTOREFRESH"]), ("False", "False"))
        # Anything else follows DEBUG, as WhiteNoise would.
        self.assertEqual(self.reported("check", DJANGO_DEBUG="False")["FINDERS"], "False")
        self.assertEqual(self.reported("check", DJANGO_DEBUG="True")["FINDERS"], "True")

    def test_only_serve_writes_the_log_file(self):
        """PROD-5: every Django process opened the same marginmate.log, and
        on Windows a second one holding it made the rotation fail - every
        record past the size lost."""
        self.assertEqual(self.reported("serve", "--verifier")["LOG_FILE"], "True")
        self.assertEqual(self.reported("serve")["LOG_FILE"], "True")
        for command in (("runserver", "8001"), ("migrate_tenants",), ("check",), ("shell",)):
            with self.subTest(command=command[0]):
                self.assertEqual(self.reported(*command)["LOG_FILE"], "False")

    def test_serve_with_a_weak_key_is_refused_at_load(self):
        key = "mot-de-passe-du-bar-2026"
        result = self.run_manage("serve", DJANGO_DEBUG="True", DJANGO_SECRET_KEY=key)
        self.assertNotIn("DEBUG=", result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("la clé secrète fait moins de 50 caractères (DJANGO_DEBUG est désactivé)", result.stderr)
        self.assertNotIn(key, result.stdout + result.stderr)
