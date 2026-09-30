"""Single mode is gone (29/09/2026; security audit ANON-1). It was what a
server got when MARGINMATE_TENANCY was missing - a git clone with no .env -
and it served every page, every export and every stored file to anyone: no
login anywhere, the legacy db.sqlite3, /media/ public when DEBUG was on.

What must never come back:

* `EmptyEnvironmentTests`: the shipped config/settings.py, loaded with an
  EMPTY environment (no MARGINMATE_*, no DJANGO_* but a SECRET_KEY - with
  none, DEBUG being off by default, the settings refuse to load - and no
  .env file), keeps one
  database per espace and sends EVERY page but the public ones to the login -
  each URL of the project asked anonymously, GET and POST - and has no
  /media/ route;
* `LeftoverSettingTests`: a stale TENANCY_MODE = "single" setting opens
  nothing - every place that used to read it at call time (the login, the
  binding, the folders, the file URLs, the signup, the checks, the admin, the
  signing links' index) now ignores it;
* `TheSwitchIsGoneTests`: nothing left to read it with.

An old .env's MARGINMATE_TENANCY is refused at load unless it says « multi »
(accounts/tests/test_settings.py).
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse

from accounts import paths, tenancy
from accounts.checks import tenancy_settings
from accounts.models import SigningLink
from tests import runner

#: Run in a child process, from the project's folder, with an environment
#: holding no MARGINMATE_* and no DJANGO_* variable but the SECRET_KEY (with
#: DEBUG off by default, a server without one is refused at load:
#: accounts/tests/test_production_settings.py). argv[1]: a temporary
#: folder. Prints REPORT<json>.
SWEEP = r"""
import importlib, json, os, re, sys
from pathlib import Path

import dotenv

# A clone of the repository has no .env: nothing may be read from one (the
# developer's own .env is in this folder).
dotenv.load_dotenv = lambda *args, **kwargs: False

module = importlib.import_module("config.settings")
OLD = ("TENANCY_MODE", "MEDIA_ROOT", "MEDIA_URL", "STAFF_PRIVATE_DIR", "SCRAPE_DOWNLOAD_DIR", "DATA_BACKUP_DIR",
       "DATA_STAGING_DIR")
decided = {
    "default": str(module.DATABASES["default"]["NAME"]),
    "aliases": sorted(module.DATABASES),
    "old_names": sorted(name for name in OLD if hasattr(module, name)),
}
# What the settings decided is recorded; only where the files go moves, into
# the test's temporary folder - the project's own tenants/ and
# accounts.sqlite3 are never opened.
scratch = Path(sys.argv[1])
module.TENANTS_ROOT = scratch / "tenants"
module.DATABASES["accounts"]["NAME"] = scratch / "accounts.sqlite3"
module.LOGGING = module.logging_config(scratch / "logs")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

import django
django.setup()

from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.urls import URLResolver, get_resolver

call_command("migrate", database="accounts", verbosity=0)

SAMPLE = {"int": "1", "str": "x", "slug": "x", "path": "x/y.pdf", "uuid": "00000000-0000-0000-0000-000000000001",
          "staff_month": "2026-06"}


def walk(resolver, prefix="", namespace=""):
    for entry in resolver.url_patterns:
        pattern = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from walk(entry, pattern, f"{namespace}{entry.namespace}:" if entry.namespace else namespace)
        else:
            yield pattern, entry, f"{namespace}{entry.name or ''}"


def concrete(pattern):
    pattern = re.sub(r"<(?:(\w+):)?(\w+)>", lambda m: SAMPLE.get(m.group(1) or "str", "x"), pattern)
    pattern = pattern.replace("^", "").replace("$", "").replace("\\Z", "")
    pattern = re.sub(r"\(\?P<\w+>[^)]*\)", "x", pattern)
    return "/" + pattern


client = Client(HTTP_HOST="localhost", raise_request_exception=False)
checked, answered, public = 0, [], set()
for pattern, entry, name in walk(get_resolver()):
    if not getattr(entry.callback, "login_required", True):
        public.add(name)
        continue
    url = concrete(pattern)
    for method in ("get", "post"):
        response = getattr(client, method)(url)
        checked += 1
        where = response.headers.get("Location", "")
        if response.status_code == 302 and where.startswith(("/connexion/?next=", "/admin/login/?next=")):
            continue
        answered.append([method.upper(), url, response.status_code, where[:80]])

media = client.get("/media/invoices/2026/01/facture.pdf").status_code
connections.close_all()
print("REPORT" + json.dumps({
    "decided": decided, "checked": checked, "answered": answered, "public": sorted(public), "media": media,
}))
"""

#: The views anyone may open: the login, the logout, the signup, Django's
#: admin login, and the employee's signing pages (their link is the key). A
#: new public view is added here on purpose, or not at all.
PUBLIC_VIEWS = [
    "accounts:login",
    "accounts:logout",
    "accounts:signup",
    "admin:login",
    "staff:sign",
    "staff:sign_check_code",
    "staff:sign_copy",
    "staff:sign_document",
    "staff:sign_send_code",
    "staff:sign_submit",
    "staff:sign_unknown",
]


class EmptyEnvironmentTests(SimpleTestCase):
    def test_every_page_wants_a_login_with_no_environment_at_all(self):
        scratch = Path(tempfile.mkdtemp(prefix="marginmate-tests-empty-env-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        project = Path(settings.BASE_DIR)
        defaults = [project / "accounts.sqlite3", project / "tenants", project / "db.sqlite3", project / "logs"]
        before = [(path, path.exists(), path.stat().st_mtime if path.exists() else None) for path in defaults]
        env = {key: value for key, value in os.environ.items() if not key.startswith(("MARGINMATE_", "DJANGO_"))}
        env["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(50)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", SWEEP, str(scratch)],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        line = next((line for line in result.stdout.splitlines() if line.startswith("REPORT")), None)
        self.assertIsNotNone(line, result.stderr[-3000:])
        report = json.loads(line[len("REPORT"):])

        # What the settings decide on their own: the espaces, never a file
        # as `default`, none of single mode's settings.
        self.assertEqual(report["decided"], {"default": ":memory:", "aliases": ["accounts", "default"], "old_names": []})
        # Every URL of the project that is not public, GET and POST, sent to
        # the login - none answered, none missing its sample (a 404 would be
        # listed too).
        self.assertEqual(report["answered"], [])
        self.assertGreater(report["checked"], 500)
        self.assertEqual(report["public"], PUBLIC_VIEWS)
        # No /media/ route, DEBUG or not.
        self.assertEqual(report["media"], 404)
        # The sweep ran on the temporary folder, and touched none of the
        # project's own files.
        self.assertTrue((scratch / "accounts.sqlite3").is_file())
        after = [(path, path.exists(), path.stat().st_mtime if path.exists() else None) for path in defaults]
        self.assertEqual(after, before)


@override_settings(TENANCY_MODE="single")
class LeftoverSettingTests(TestCase):
    """Each assertion here failed while single mode existed: the setting was
    read at call time, and « single » switched the place off."""

    def test_the_login_is_still_asked(self):
        response = Client().get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login")), response["Location"])

    def test_a_request_is_still_bound_to_the_user_s_espace(self):
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.tenant.pk, runner.TEST_TENANT_PK)
        self.assertEqual(tenancy.current_tenant().pk, runner.TEST_TENANT_PK)

    def test_the_folders_are_still_the_espace_s(self):
        self.assertEqual(paths.media_root(), paths.tenant_dir(runner.TEST_TENANT) / paths.MEDIA)
        self.assertEqual(paths.imports_dir(), paths.tenant_dir(runner.TEST_TENANT) / paths.IMPORTS)

    def test_a_stored_file_is_still_reached_through_the_logged_in_view(self):
        self.assertEqual(default_storage.url("invoices/2026/01/facture.pdf"), "/fichiers/invoices/2026/01/facture.pdf")

    def test_the_signup_is_still_open(self):
        self.assertEqual(Client().get(reverse("accounts:signup")).status_code, 200)

    def test_the_checks_still_run(self):
        with override_settings(TENANTS_ROOT=""):
            self.assertIn("accounts.E002", [issue.id for issue in tenancy_settings()])

    def test_the_admin_is_still_for_superusers_only(self):
        staff = runner.member_of_the_test_espace(
            get_user_model().objects.create_user(
                username="equipe@example.invalid", email="equipe@example.invalid", is_staff=True
            )
        )
        self.client.force_login(staff)
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"), response["Location"])

    def test_the_signing_links_index_is_still_kept(self):
        from accounts import links

        links.register("a" * 64)
        self.assertEqual(SigningLink.objects.get(token_hash="a" * 64).tenant_id, runner.TEST_TENANT_PK)
        self.assertEqual(links.resolve("a" * 64).pk, runner.TEST_TENANT_PK)


class TheSwitchIsGoneTests(SimpleTestCase):
    def test_nothing_is_left_to_read_a_mode_with(self):
        for name in ("multi_mode", "mode", "HOUSE", "SINGLE", "MULTI", "MODES"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tenancy, name))
        self.assertFalse(hasattr(settings, "TENANCY_MODE"))

    def test_there_is_no_media_route(self):
        with self.assertRaises(Resolver404):
            resolve("/media/invoices/2026/01/facture.pdf")
