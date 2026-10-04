"""The production settings (config/settings.py, config/security.py,
config/logs.py) and the server's system checks (accounts/checks.py):
security audit ANON-2, DEPLOY-1, LB-1 (DEBUG), DEPLOY-2, ANON-7 (the
SECRET_KEY), ANON-3, DEPLOY-3 (HTTPS), DEPLOY-5 and ANON-6's log half (the
logs).

The real settings module is loaded in child processes that read NO .env
(the developer's own is in the project's folder) and whose every path is a
temporary one; the checks are also called in this process with the settings
overridden. Hosts and addresses invented.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning, run_checks
from django.test import SimpleTestCase, TestCase, override_settings

from accounts import checks
from config.security import (
    DEVELOPMENT_SECRET_KEY,
    PUBLIC_SECRET_KEYS,
    secret_key_problem,
)

PROBE = r"""
import json
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import django
django.setup()
from django.conf import settings
from django.core import checks


def ids(deploy):
    return sorted(
        message.id for message in checks.run_checks(include_deployment_checks=deploy)
        if message.level >= checks.WARNING and not message.is_silenced()
    )


names = (
    "DEBUG", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD", "SECURE_SSL_REDIRECT", "SECURE_PROXY_SSL_HEADER", "CSRF_TRUSTED_ORIGINS", "ALLOWED_HOSTS",
    "SESSION_COOKIE_HTTPONLY", "CSRF_COOKIE_HTTPONLY", "X_FRAME_OPTIONS", "SECURE_REFERRER_POLICY",
    "SECURE_CONTENT_TYPE_NOSNIFF", "SECURE_CROSS_ORIGIN_OPENER_POLICY", "MIDDLEWARE",
)
report = {name: getattr(settings, name) for name in names}
report["development_key"] = settings.SECRET_KEY == "django-insecure-dev-key-change-me"
report["LOG_DIR"] = str(getattr(settings, "LOG_DIR", ""))
report["checks"] = ids(False)
report["deploy"] = ids(True)
print("REPORT" + json.dumps(report))
"""

#: The logs, in a child whose settings are production's: a view that fails,
#: a refused CSRF token, a forged Host, an unknown signing address - and a
#: line naming a signing link. argv[1]: a token; argv[2]: the manage.py
#: command the process stands for (the settings read sys.argv as manage.py
#: does: only `serve` writes the log file).
LOGGED = r"""
import json
import logging
import os
import sys
import types
from pathlib import Path

token, command = sys.argv[1], sys.argv[2]
sys.argv = ["manage.py", command]

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import django
django.setup()
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.test import Client
from django.urls import include, path

folder = Path(os.environ["MARGINMATE_LOG_DIR"])
made_at_start = folder.exists()


def breaks(request):
    raise RuntimeError(f"panne d'essai sur /personnel/signer/{token}/document/")


urls = types.ModuleType("essai_urls")
urls.urlpatterns = [path("essai-panne/", login_not_required(breaks)), path("", include("config.urls"))]
sys.modules["essai_urls"] = urls
settings.ROOT_URLCONF = "essai_urls"

answers = {
    "500": Client(HTTP_HOST="localhost", raise_request_exception=False).get("/essai-panne/"),
    "csrf": Client(HTTP_HOST="localhost", enforce_csrf_checks=True).post("/connexion/", {"username": "a@example.invalid"}),
    "host": Client(HTTP_HOST="ailleurs.example").get("/connexion/"),
    "404": Client(HTTP_HOST="localhost").get(f"/personnel/signer/{token}/pas-une-page/"),
}
logging.getLogger("django.request").error("Not Found: %s", f"/personnel/signer/{token}/exemplaire/")
for handler in logging.getLogger().handlers:
    handler.flush()
log = folder / "marginmate.log"
print("REPORT" + json.dumps({
    "made_at_start": made_at_start,
    "made_at_end": folder.exists(),
    "status": {name: answer.status_code for name, answer in answers.items()},
    "pages": {name: answer.content.decode("utf-8", "replace") for name, answer in answers.items()},
    "log": log.read_text(encoding="utf-8") if log.is_file() else None,
}))
"""


def deploy_md_lines() -> dict:
    """The .env lines DEPLOY.md gives (its section 5's block), the domain
    replaced by an invented one."""
    text = (Path(settings.BASE_DIR) / "DEPLOY.md").read_text(encoding="utf-8")
    section = text.split("## 5.", 1)[1]
    block = section.split("```", 2)[1]
    lines = {}
    for line in block.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            lines[name.strip()] = value.strip().replace("<votre-domaine>", "example.com")
    return lines


#: A server set up as DEPLOY.md says, with what the owner's .env already
#: holds (a strong key is set by `run_child`; the passphrase here).
ONLINE = {**deploy_md_lines(), "MARGINMATE_SIGNING_PASSPHRASE": "phrase-de-passe-d-essai"}


#: What a child process keeps of this one's environment: what the system
#: needs to run Python, nothing else. This process's environment holds the
#: developer's whole .env (load_dotenv put it there) - credentials a child
#: must neither read nor show on a debug page.
SYSTEM_VARIABLES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
    }
)


def child_environment(**environment) -> dict:
    """The environment of a child loading the real settings: the system's
    variables, then `environment` (a None value removes the variable)."""
    env = {key: value for key, value in os.environ.items() if key.upper() in SYSTEM_VARIABLES}
    env.update(environment)
    return {key: value for key, value in env.items() if value is not None}


class ChildTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-production-"))

    def run_child(self, code, *args, **environment):
        env = child_environment()
        env.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
                "MARGINMATE_TENANTS_ROOT": str(self.folder / "espaces"),
                "MARGINMATE_ACCOUNTS_DB": str(self.folder / "comptes.sqlite3"),
                "MARGINMATE_LOG_DIR": str(self.folder / "journal"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.update(environment)
        for name, value in environment.items():
            if value is None:
                env.pop(name)
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def probe(self, **environment) -> dict:
        result = self.run_child(PROBE, **environment)
        line = next((line for line in result.stdout.splitlines() if line.startswith("REPORT")), None)
        self.assertIsNotNone(line, result.stderr[-3000:])
        return json.loads(line[len("REPORT") :])

    def refused(self, **environment) -> str:
        result = self.run_child(PROBE, **environment)
        self.assertNotIn("REPORT", result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)
        # Refused at load: nothing made.
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [])
        return result.stdout + result.stderr


class DebugAndKeyTests(ChildTestCase):
    def test_debug_is_off_unless_the_environment_turns_it_on(self):
        """ANON-2, DEPLOY-1, LB-1: it defaulted to True - a server started
        without DJANGO_DEBUG showed Django's technical pages to anybody."""
        self.assertIs(self.probe()["DEBUG"], False)
        self.assertIs(self.probe(DJANGO_DEBUG="True")["DEBUG"], True)
        self.assertIs(self.probe(DJANGO_DEBUG="False")["DEBUG"], False)

    def test_debug_off_a_missing_key_is_refused_at_load(self):
        """DEPLOY-2: no more public fallback key on a server."""
        said = self.refused(DJANGO_SECRET_KEY=None)
        self.assertIn("DJANGO_SECRET_KEY n'est pas définie (DJANGO_DEBUG est désactivé)", said)
        self.assertIn("get_random_secret_key", said)

    def test_debug_off_a_weak_key_is_refused_at_load_and_never_printed(self):
        for key in (
            "change-me-to-a-random-secret",
            DEVELOPMENT_SECRET_KEY,
            "django-insecure-" + secrets.token_urlsafe(50),
            "mot-de-passe-du-bar-2026",
            "ab" * 40,
        ):
            with self.subTest(key=key[:12]):
                said = self.refused(DJANGO_SECRET_KEY=key)
                self.assertIn("(DJANGO_DEBUG est désactivé)", said)
                self.assertNotIn(key, said)

    def test_on_a_developer_s_machine_the_development_key_is_named(self):
        report = self.probe(DJANGO_DEBUG="True", DJANGO_SECRET_KEY=None)
        self.assertIs(report["development_key"], True)
        self.assertIn("accounts.W001", report["checks"])

    def test_debug_beside_a_public_host_is_refused(self):
        report = self.probe(DJANGO_DEBUG="True", DJANGO_ALLOWED_HOSTS="gestion.example.com,localhost")
        self.assertIn("accounts.E007", report["checks"])
        self.assertNotIn("accounts.E007", self.probe(DJANGO_DEBUG="True")["checks"])


#: What a request's end (Django's close_old_connections, sent by the WSGI
#: handler Waitress runs) leaves of each connection, under production's
#: settings: the accounts one, and a tenant's made by accounts.tenancy.
KEPT = r"""
import json
import sys
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import django
django.setup()
from django.db import close_old_connections, connections
from accounts.tenancy import _wrapper_for

accounts = connections["accounts"]
accounts.ensure_connection()
before = accounts.connection
close_old_connections()
tenant = _wrapper_for(sys.argv[1])
print("REPORT" + json.dumps({
    "accounts_kept": accounts.connection is not None and accounts.connection is before,
    "accounts_health_checks": accounts.settings_dict["CONN_HEALTH_CHECKS"],
    "tenant_max_age": tenant.settings_dict["CONN_MAX_AGE"],
}))
"""


class ConnectionsTests(ChildTestCase):
    def test_the_accounts_connection_outlives_a_request_and_a_tenant_s_does_not(self):
        """Every request reads its session, login and membership in the
        accounts file: closed at each request's end, the next one opened it
        again - its PRAGMAs, and the -wal checkpointed, deleted and made
        again, 1.5-3 ms a request on Windows. It holds no bar's rows; a
        tenant's connection is still closed (the binding's, CLAUDE.md)."""
        result = self.run_child(KEPT, str(self.folder / "espace.sqlite3"))
        line = next((line for line in result.stdout.splitlines() if line.startswith("REPORT")), None)
        self.assertIsNotNone(line, result.stderr[-3000:])
        report = json.loads(line[len("REPORT") :])
        self.assertEqual(report, {"accounts_kept": True, "accounts_health_checks": True, "tenant_max_age": 0})


class HttpsTests(ChildTestCase):
    def test_off_by_default(self):
        report = self.probe()
        self.assertIs(report["SESSION_COOKIE_SECURE"], False)
        self.assertIs(report["CSRF_COOKIE_SECURE"], False)
        self.assertEqual(report["SECURE_HSTS_SECONDS"], 0)
        self.assertEqual(report["CSRF_TRUSTED_ORIGINS"], [])

    def test_marginmate_https_turns_it_on(self):
        """ANON-3, DEPLOY-3: nothing could turn secure cookies or HSTS on."""
        report = self.probe(MARGINMATE_HTTPS="1", DJANGO_CSRF_TRUSTED_ORIGINS=" https://gestion.example.com , ")
        self.assertIs(report["SESSION_COOKIE_SECURE"], True)
        self.assertIs(report["CSRF_COOKIE_SECURE"], True)
        self.assertEqual(report["SECURE_HSTS_SECONDS"], 3600)
        self.assertIs(report["SECURE_HSTS_INCLUDE_SUBDOMAINS"], False)
        self.assertIs(report["SECURE_HSTS_PRELOAD"], False)
        # Cloudflare redirects; Waitress, not Django, trusts the proxy.
        self.assertIs(report["SECURE_SSL_REDIRECT"], False)
        self.assertIsNone(report["SECURE_PROXY_SSL_HEADER"])
        self.assertEqual(report["CSRF_TRUSTED_ORIGINS"], ["https://gestion.example.com"])
        self.assertIs(report["SESSION_COOKIE_HTTPONLY"], True)
        # htmx reads it (base.html).
        self.assertIs(report["CSRF_COOKIE_HTTPONLY"], False)
        self.assertEqual(report["X_FRAME_OPTIONS"], "DENY")
        self.assertEqual(report["SECURE_REFERRER_POLICY"], "same-origin")
        self.assertIs(report["SECURE_CONTENT_TYPE_NOSNIFF"], True)
        self.assertEqual(report["SECURE_CROSS_ORIGIN_OPENER_POLICY"], "same-origin")

    def test_hsts_is_raised_by_the_environment(self):
        self.assertEqual(
            self.probe(MARGINMATE_HTTPS="1", MARGINMATE_HSTS_SECONDS="31536000")["SECURE_HSTS_SECONDS"], 31536000
        )
        said = self.refused(MARGINMATE_HTTPS="1", MARGINMATE_HSTS_SECONDS="une heure")
        self.assertIn("MARGINMATE_HSTS_SECONDS", said)

    def test_the_middleware(self):
        middleware = self.probe()["MIDDLEWARE"]
        self.assertEqual(
            middleware[:3],
            [
                "django.middleware.security.SecurityMiddleware",
                "whitenoise.middleware.WhiteNoiseMiddleware",
                "config.security.ContentSecurityPolicyMiddleware",
            ],
        )
        self.assertLess(
            middleware.index("config.security.ContentSecurityPolicyMiddleware"),
            middleware.index("django.middleware.clickjacking.XFrameOptionsMiddleware"),
        )


class DeploymentChecksTests(ChildTestCase):
    def test_deploy_md_gives_the_lines_a_server_needs(self):
        self.assertEqual(
            deploy_md_lines(),
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_ALLOWED_HOSTS": "gestion.example.com,localhost,127.0.0.1",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://gestion.example.com",
                "MARGINMATE_HTTPS": "1",
                "MARGINMATE_SITE_URL": "https://gestion.example.com",
            },
        )

    def test_a_server_set_up_as_deploy_md_says_passes_check_deploy(self):
        """What `manage.py serve` asks before it starts - no warning at all."""
        report = self.probe(**ONLINE)
        self.assertEqual(report["deploy"], [])
        self.assertIs(report["DEBUG"], False)
        self.assertIs(report["SESSION_COOKIE_SECURE"], True)

    def test_what_is_missing_is_named(self):
        report = self.probe(**{**ONLINE, "MARGINMATE_HTTPS": None})
        for issue in ("accounts.E009", "security.W004", "security.W012", "security.W016"):
            self.assertIn(issue, report["deploy"])
        report = self.probe(**{**ONLINE, "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1"})
        self.assertIn("accounts.E008", report["deploy"])
        self.assertIn("accounts.E010", report["deploy"])
        report = self.probe(**{**ONLINE, "MARGINMATE_SIGNING_PASSPHRASE": None})
        self.assertIn("staff.W001", report["deploy"])
        # The tenants left at their default, beside manage.py.
        report = self.probe(**{**ONLINE, "MARGINMATE_TENANTS_ROOT": None, "MARGINMATE_LOG_DIR": None})
        self.assertIn("accounts.E011", report["deploy"])
        # The deployment checks are not everyday's: a developer's check
        # stays quiet.
        self.assertNotIn("accounts.E009", self.probe()["checks"])


class LogsTests(ChildTestCase):
    """DEPLOY-5: with DEBUG off and no LOGGING, a 500's traceback, a CSRF
    refusal and a forged Host left no trace. ANON-6: the signing link's
    token was written whole into the log."""

    def logged(self, token, command):
        result = self.run_child(LOGGED, token, command)
        line = next((line for line in result.stdout.splitlines() if line.startswith("REPORT")), None)
        self.assertIsNotNone(line, result.stderr[-3000:])
        return result, json.loads(line[len("REPORT") :])

    def test_any_other_process_logs_to_its_console_only(self):
        """PROD-5: every process opened the same marginmate.log, and on
        Windows a second one holding it (a debug runserver, a long command)
        made the rotation fail: every record past the size was lost. The
        file is the production server's alone."""
        token = secrets.token_urlsafe(32)
        for command in ("runserver", "migrate_tenants"):
            with self.subTest(command=command):
                result, report = self.logged(token, command)
                self.assertEqual(report["status"], {"500": 500, "csrf": 403, "host": 400, "404": 404})
                self.assertIsNone(report["log"])
                self.assertIs(report["made_at_end"], False)
                # Said all the same, in its own window, the links cut short.
                self.assertIn("Internal Server Error: /essai-panne/", result.stderr)
                self.assertIn("django.security.DisallowedHost", result.stderr)
                self.assertIn(f"/personnel/signer/{token[:4]}\N{HORIZONTAL ELLIPSIS}/exemplaire/", result.stderr)
                self.assertNotIn(token, result.stderr)

    def test_errors_and_refusals_are_logged_with_the_links_cut_short(self):
        token = secrets.token_urlsafe(32)
        result, report = self.logged(token, "serve")
        self.assertIs(report["made_at_start"], False)
        self.assertEqual(report["status"], {"500": 500, "csrf": 403, "host": 400, "404": 404})
        log = report["log"]
        self.assertIsNotNone(log, "no log file")
        # The 500 and its traceback, the CSRF refusal, the forged Host.
        self.assertIn("ERROR django.request : Internal Server Error: /essai-panne/", log)
        self.assertIn("Traceback", log)
        self.assertIn("RuntimeError", log)
        self.assertIn("WARNING django.security.csrf : Forbidden (", log)
        self.assertIn("django.security.DisallowedHost", log)
        # A 404 is no error: not written.
        self.assertNotIn("pas-une-page", log)
        # Every signing link cut to 4 characters, in the file and on the console.
        self.assertIn(f"/personnel/signer/{token[:4]}\N{HORIZONTAL ELLIPSIS}/document/", log)
        self.assertIn(f"Not Found: /personnel/signer/{token[:4]}\N{HORIZONTAL ELLIPSIS}/exemplaire/", log)
        self.assertNotIn(token, log)
        self.assertIn("Internal Server Error: /essai-panne/", result.stderr)
        self.assertNotIn(token, result.stderr)
        # The visitor saw none of it.
        for name, page in report["pages"].items():
            with self.subTest(page=name):
                self.assertNotIn("RuntimeError", page)
                self.assertNotIn("Traceback", page)
        self.assertIn("<h1>Erreur du serveur</h1>", report["pages"]["500"])
        self.assertIn("<h1>La page a expiré</h1>", report["pages"]["csrf"])
        self.assertIn("<h1>Demande incorrecte</h1>", report["pages"]["host"])


class SecretKeyRulesTests(SimpleTestCase):
    def test_what_is_weak(self):
        self.assertEqual(secret_key_problem(secrets.token_urlsafe(50)), "")
        for key, words in (
            ("", "n'est pas définie"),
            ("   ", "n'est pas définie"),
            ("change-me-to-a-random-secret", "valeur publique"),
            (DEVELOPMENT_SECRET_KEY, "valeur publique"),
            ("django-insecure-" + "x1y2z3" * 10, "django-insecure"),
            ("court", "moins de 50 caractères"),
            ("ab" * 40, "moins de 5 caractères différents"),
        ):
            with self.subTest(key=key[:10]):
                self.assertIn(words, secret_key_problem(key))
        self.assertIn("change-me-to-a-random-secret", PUBLIC_SECRET_KEYS)

    def test_the_example_file_holds_no_key(self):
        """.env.example is public: its key was the one the owner's .env held."""
        example = (Path(settings.BASE_DIR) / ".env.example").read_text(encoding="utf-8")
        self.assertIn("\nDJANGO_SECRET_KEY=\n", example)
        self.assertIn("\nDJANGO_DEBUG=False\n", example)
        for name in (
            "MARGINMATE_HTTPS=",
            "DJANGO_CSRF_TRUSTED_ORIGINS=",
            "MARGINMATE_LOG_DIR=",
            "MARGINMATE_HSTS_SECONDS=",
        ):
            self.assertIn(f"\n{name}\n", example)

    def test_the_example_file_leaves_the_fuzzy_threshold_to_the_settings(self):
        """A .env copied from the example ran the product matcher at 92, the
        value config/settings.py raised to 94 because a longer « COCA COLA
        … » line merged silently into its « ZERO »: a value in .env wins
        over the measured default."""
        example = (Path(settings.BASE_DIR) / ".env.example").read_text(encoding="utf-8")
        set_lines = [line for line in example.splitlines() if line.startswith("PRODUCT_FUZZY_MATCH_THRESHOLD=")]
        self.assertEqual(set_lines, [])


class ServerChecksTests(TestCase):
    """The checks called in this process, settings overridden. A TestCase:
    run_checks also runs accounts.E005, which reads the accounts database."""

    def ids(self, issues):
        return [issue.id for issue in issues]

    def test_a_weak_key_is_an_error_with_debug_off_a_warning_with_it_on(self):
        with override_settings(SECRET_KEY="mot-de-passe-du-bar-2026", DEBUG=False):
            (issue,) = checks.secret_key()
            self.assertIsInstance(issue, Error)
            self.assertEqual(issue.id, "accounts.E006")
            self.assertNotIn("mot-de-passe-du-bar-2026", issue.msg + issue.hint)
            self.assertIn("accounts.E006", self.ids(run_checks()))
        with override_settings(SECRET_KEY="mot-de-passe-du-bar-2026", DEBUG=True):
            (issue,) = checks.secret_key()
            self.assertIsInstance(issue, Warning)
            self.assertEqual(issue.id, "accounts.W001")
        with override_settings(SECRET_KEY=secrets.token_urlsafe(50), DEBUG=False):
            self.assertEqual(checks.secret_key(), [])

    def test_debug_beside_a_public_host(self):
        with override_settings(DEBUG=True, ALLOWED_HOSTS=["localhost", "gestion.example.com"]):
            (issue,) = checks.debug_on_a_public_host()
            self.assertIsInstance(issue, Error)
            self.assertEqual(issue.id, "accounts.E007")
            self.assertIn("gestion.example.com", issue.msg)
            self.assertIn("DJANGO_DEBUG=False", issue.hint)
        for hosts in (["localhost", "127.0.0.1", "[::1]", "testserver", "app.localhost"], []):
            with self.subTest(hosts=hosts), override_settings(DEBUG=True, ALLOWED_HOSTS=hosts):
                self.assertEqual(checks.debug_on_a_public_host(), [])
        with override_settings(DEBUG=True, ALLOWED_HOSTS=["*"]):
            self.assertEqual(self.ids(checks.debug_on_a_public_host()), ["accounts.E007"])
        with override_settings(DEBUG=False, ALLOWED_HOSTS=["gestion.example.com"]):
            self.assertEqual(checks.debug_on_a_public_host(), [])

    def test_the_hosts_to_serve(self):
        for hosts, words in (([], "aucun hôte public"), (["localhost"], "aucun hôte public"), (["*"], "« * »")):
            with self.subTest(hosts=hosts), override_settings(ALLOWED_HOSTS=hosts):
                (issue,) = checks.allowed_hosts_online()
                self.assertEqual(issue.id, "accounts.E008")
                self.assertIn(words, issue.msg)
        with override_settings(ALLOWED_HOSTS=["gestion.example.com", "localhost"]):
            self.assertEqual(checks.allowed_hosts_online(), [])

    def test_the_cookies_to_serve(self):
        for session, csrf in ((False, False), (True, False), (False, True)):
            with (
                self.subTest(session=session, csrf=csrf),
                override_settings(SESSION_COOKIE_SECURE=session, CSRF_COOKIE_SECURE=csrf),
            ):
                self.assertEqual(self.ids(checks.https_cookies_online()), ["accounts.E009"])
        with override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True):
            self.assertEqual(checks.https_cookies_online(), [])

    def test_the_signing_links_address(self):
        hosts = ["gestion.example.com", "localhost"]
        for url in ("http://gestion.example.com", "https://ailleurs.example.com", "gestion.example.com", "https://"):
            with self.subTest(url=url), override_settings(SITE_URL=url, ALLOWED_HOSTS=hosts):
                self.assertEqual(self.ids(checks.site_url_online()), ["accounts.E010"])
        for url in ("", "https://gestion.example.com", "https://gestion.example.com:8443"):
            with self.subTest(url=url), override_settings(SITE_URL=url, ALLOWED_HOSTS=hosts):
                self.assertEqual(checks.site_url_online(), [])

    def test_the_data_outside_the_code(self):
        base = Path(settings.BASE_DIR)
        with override_settings(TENANTS_ROOT=base / "tenants"):
            (issue,) = checks.data_outside_the_code()
        self.assertEqual(issue.id, "accounts.E011")
        self.assertIn("MARGINMATE_TENANTS_ROOT", issue.msg)
        self.assertNotIn("MARGINMATE_ACCOUNTS_DB", issue.msg)
        databases = {
            **settings.DATABASES,
            "accounts": {**settings.DATABASES["accounts"], "NAME": base / "accounts.sqlite3"},
        }
        with warnings.catch_warnings():
            # Overriding DATABASES warns: only this check reads it here.
            warnings.simplefilter("ignore")
            with override_settings(DATABASES=databases):
                (issue,) = checks.data_outside_the_code()
        self.assertIn("MARGINMATE_ACCOUNTS_DB", issue.msg)
        # Beside the code's folder, as the owner's data/ is: nothing to say.
        with override_settings(TENANTS_ROOT=base.parent / "data" / "tenants"):
            self.assertEqual(checks.data_outside_the_code(), [])

    def test_the_server_s_checks_are_deployment_ones(self):
        with override_settings(ALLOWED_HOSTS=["localhost"], SESSION_COOKIE_SECURE=False, SITE_URL="http://x.example"):
            everyday = self.ids(run_checks())
            deploy = self.ids(run_checks(include_deployment_checks=True))
        for issue in ("accounts.E008", "accounts.E009", "accounts.E010"):
            self.assertNotIn(issue, everyday)
            self.assertIn(issue, deploy)

    def test_the_limiter_s_cache_warning_says_one_process_is_fine(self):
        with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}):
            (issue,) = checks.limiter_cache()
        self.assertIn("LocMemCache", issue.hint)
        self.assertIn("manage.py serve, which is one process", issue.hint)
