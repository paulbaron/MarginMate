"""The settings (config/settings.py) and their system checks
(accounts/checks.py). One database per espace, a login on every page - the
only mode since single mode was removed (29/09/2026).

The real settings module is loaded in a child process, with every path set
by the environment - temporary paths: nothing here opens or creates a
database in the project's folder.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from accounts.checks import PUBLIC_SECRET_KEY, tenancy_settings

PROBE = r"""
import json, django
django.setup()
from django.conf import settings
from django.core import checks
from django.db import OperationalError
report = {
    "old_switch": hasattr(settings, "TENANCY_MODE"),
    "default": str(settings.DATABASES["default"]["NAME"]),
    "accounts": str(settings.DATABASES.get("accounts", {}).get("NAME", "")),
    "tenants_root": str(settings.TENANTS_ROOT),
    "storage": settings.STORAGES["default"]["BACKEND"],
    "routers": settings.DATABASE_ROUTERS,
    "middleware": settings.MIDDLEWARE,
    "login_url": settings.LOGIN_URL,
    "checks": sorted(message.id for message in checks.run_checks() if message.id and message.id.startswith("accounts.")),
}
from invoices.models import Supplier
try:
    Supplier.objects.count()
    report["unbound"] = "answered"
except OperationalError as exc:
    report["unbound"] = str(exc)
print("REPORT" + json.dumps(report))
"""


def load_settings(**environment) -> subprocess.CompletedProcess:
    """PROBE run in a child process on the real config/settings.py, with
    `environment` over the test process's own."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("MARGINMATE_")}
    # Dropped from the environment, these would still be read back from the
    # owner's .env (load_dotenv never overrides a variable that is SET), and
    # his own values would decide. Set to "" they are set, so .env cannot
    # fill them.
    for name in set(re.findall(r"MARGINMATE_[A-Z_]+", (Path(settings.BASE_DIR) / "config" / "settings.py").read_text(encoding="utf-8"))):
        env[name] = ""
    # The developer's .env went into this process's environment: what it
    # says about DEBUG and the hosts must not decide (a server's .env names
    # a public host, which DEBUG on refuses: accounts.E007).
    env["DJANGO_DEBUG"] = "False"
    env["DJANGO_ALLOWED_HOSTS"] = "localhost,127.0.0.1"
    env.update(environment)
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    # The refusals are French: read back as written, whatever the console's.
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def probe(**environment) -> dict:
    result = load_settings(**environment)
    line = next((line for line in result.stdout.splitlines() if line.startswith("REPORT")), None)
    if line is None:
        raise AssertionError(result.stderr[-3000:])
    return json.loads(line[len("REPORT"):])


class SettingsFromTheEnvironmentTests(SimpleTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-settings-"))
        self.environment = {
            "MARGINMATE_TENANTS_ROOT": str(self.folder / "espaces"),
            "MARGINMATE_ACCOUNTS_DB": str(self.folder / "comptes.sqlite3"),
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
        }

    def test_one_database_per_espace_with_no_switch_at_all(self):
        """No MARGINMATE_TENANCY: the espaces and the login all the same -
        single mode was what a missing switch used to mean."""
        report = probe(**self.environment)
        self.assertFalse(report["old_switch"])
        self.assertEqual(report["default"], ":memory:")
        self.assertEqual(report["accounts"], str(self.folder / "comptes.sqlite3"))
        self.assertEqual(report["tenants_root"], str(self.folder / "espaces"))
        self.assertEqual(report["storage"], "accounts.storage.TenantFileSystemStorage")
        self.assertEqual(report["routers"], ["accounts.router.AccountsRouter"])
        self.assertEqual(report["login_url"], "/connexion/")
        middleware = report["middleware"]
        auth = middleware.index("django.contrib.auth.middleware.AuthenticationMiddleware")
        self.assertEqual(
            middleware[auth + 1:auth + 3],
            ["accounts.middleware.LoginRequiredMiddleware", "accounts.middleware.TenantMiddleware"],
        )
        self.assertEqual(report["checks"], [])
        # Unbound, a business query fails loudly: nothing to land in.
        self.assertIn("no such table", report["unbound"])
        # Loading the settings created nothing.
        self.assertFalse((self.folder / "comptes.sqlite3").exists())
        self.assertFalse((self.folder / "espaces").exists())

    def test_an_old_env_saying_multi_changes_nothing(self):
        """The owner's .env still says MARGINMATE_TENANCY=multi: ignored."""
        self.assertEqual(probe(MARGINMATE_TENANCY="multi", **self.environment), probe(**self.environment))
        self.assertEqual(probe(MARGINMATE_TENANCY=" Multi ", **self.environment)["default"], ":memory:")

    def test_an_old_env_asking_for_single_mode_is_refused_at_load(self):
        """Refused by the settings themselves, not by a system check: a WSGI
        server runs none, and single mode started on it with no login. A
        typo is refused alike - it used to mean single mode too."""
        for value in ("single", "SINGLE", "mutli", "both"):
            with self.subTest(value=value):
                result = load_settings(MARGINMATE_TENANCY=value, **self.environment)
                self.assertNotIn("REPORT", result.stdout)
                self.assertIn("ImproperlyConfigured", result.stderr)
                self.assertIn("le mode « single » (une seule base, aucune connexion) n'existe plus", result.stderr)
                self.assertIn("Retirez la ligne MARGINMATE_TENANCY", result.stderr)
        self.assertFalse((self.folder / "comptes.sqlite3").exists())

    def test_the_public_secret_key_is_warned_about(self):
        """On a developer's machine (DEBUG on) a warning; anywhere else the
        settings refuse to load (accounts/tests/test_production_settings.py)."""
        report = probe(**{**self.environment, "DJANGO_SECRET_KEY": PUBLIC_SECRET_KEY, "DJANGO_DEBUG": "True"})
        self.assertEqual(report["checks"], ["accounts.W001"])
        result = load_settings(**{**self.environment, "DJANGO_SECRET_KEY": PUBLIC_SECRET_KEY})
        self.assertNotIn("REPORT", result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)


class ChecksTests(SimpleTestCase):
    def test_a_leftover_mode_setting_silences_no_check(self):
        """Single mode's checks said nothing at all: TENANCY_MODE is read
        by nobody now, whatever a stale settings file sets."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for mode in ("single", "both"):
                with self.subTest(mode=mode), override_settings(TENANCY_MODE=mode, TENANTS_ROOT=""):
                    self.assertIn("accounts.E002", [issue.id for issue in tenancy_settings()])

    def test_the_settings_need_their_folders_and_databases(self):
        memory = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        cases = [
            ({"TENANTS_ROOT": "", "DATABASES": {"default": memory, "accounts": {"NAME": "c.sqlite3"}}}, ["accounts.E002"]),
            ({"TENANTS_ROOT": "espaces", "DATABASES": {"default": memory}}, ["accounts.E003"]),
            (
                {"TENANTS_ROOT": "espaces", "DATABASES": {"default": {"NAME": "vrai.sqlite3"}, "accounts": {"NAME": "c.sqlite3"}}},
                ["accounts.E004"],
            ),
            ({"TENANTS_ROOT": "espaces", "DATABASES": {"default": memory, "accounts": {"NAME": "c.sqlite3"}}}, []),
        ]
        for overrides, expected in cases:
            with self.subTest(expected=expected), warnings.catch_warnings():
                # Overriding DATABASES warns: only this check reads it here.
                warnings.simplefilter("ignore")
                with override_settings(SECRET_KEY=secrets.token_urlsafe(50), **overrides):
                    self.assertEqual([issue.id for issue in tenancy_settings()], expected)

    def test_the_test_suite_runs_as_production_does(self):
        """tests/runner.py: its own settings pass the checks made at every
        start, and carry no mode switch."""
        self.assertFalse(hasattr(settings, "TENANCY_MODE"))
        self.assertEqual(settings.TEST_RUNNER, "tests.runner.TenantTestRunner")
        self.assertIn("accounts", settings.DATABASES)
        self.assertEqual([issue.id for issue in tenancy_settings() if issue.is_serious()], [])
