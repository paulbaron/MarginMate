"""Settings used by the test suite: `manage.py test --settings=config.settings_test`.

Three jobs beyond speed. First, it makes it *impossible* for a test to reach a
real service by accident: every credential is blanked, so the IMAP fetcher,
the Metro scraper and the LLM parser all refuse to start rather than dialling
out with the developer's real .env values. Second, it pins anything a test's
outcome could otherwise inherit from the local .env (notably the fuzzy-match
threshold), so a passing suite means the same thing on every machine. Third,
the suite runs the application the way production does - there is no other
way left since single mode was removed: every page behind the login, every
file in a tenant's own folders.

How (tests/runner.py): the test runner's `default` IS the database of one
« test tenant », whose rows - the tenant, its owner's login, his membership -
live in the `accounts` test database, as the central rows do in production.
Every test client is logged in as that owner from its first request, and
every folder a test reads or writes is the tenant's own
(`accounts.paths.*`), under the temporary TENANTS_ROOT below.
"""

import os
import tempfile
from pathlib import Path

# A key of the suite's own, set BEFORE config/settings.py loads the .env
# (load_dotenv never overrides a variable already set): no test signs
# anything with the developer's real key, and a clone with no .env - where
# DEBUG is off by default - is not refused for a missing one. Public, like
# everything in this file: tests only.
os.environ.setdefault(
    "DJANGO_SECRET_KEY", "tests-seulement-cle-publique-du-jeu-d-essai-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

from .settings import *

# Every tenant's folder, the test tenant's included - never the owner's
# (the .env may name his; this wins).
TENANTS_ROOT = Path(tempfile.mkdtemp(prefix="marginmate-tests-tenants-"))

# Two in-memory test databases, split between them by the router as in
# production: `default` is the test tenant's data (the runner never swaps
# it), `accounts` the logins, sessions and tenants. Not a TEST MIRROR of one
# another: a mirror is a second connection to the same database, and the
# router would never create the central tables on it.
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "accounts": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}

TEST_RUNNER = "tests.runner.TenantTestRunner"

# The warnings about two settings the test run blanks on purpose: the
# development SECRET_KEY (accounts.W001) and the signing passphrase
# (staff.W001, below) - besides what production silences itself.
SILENCED_SYSTEM_CHECKS = [*SILENCED_SYSTEM_CHECKS, "accounts.W001", "staff.W001"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

DEBUG = False

# Production's settings, pinned whatever the developer's .env says about
# the server (its public host, HTTPS): a test's outcome never depends on it.
# The tests of production itself set them (accounts/tests/test_serve.py,
# test_production_settings.py).
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = []
HTTPS = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
# Django's own logging, not production's (config/logs.py): its file is the
# owner's data folder by default, and the suite causes 500s on purpose.
LOGGING = {}
# /static/ from the apps' folders, looked up at each request: a test run has
# no collectstatic, and a STATIC_ROOT that does not exist would be warned
# about by every client the suite makes.
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

# No credentials => the external integrations raise instead of connecting.
# See tests.support.NoNetworkTestCase for the belt-and-braces patching.
INVOICE_EMAIL_ADDRESS = ""
INVOICE_EMAIL_APP_PASSWORD = ""
UBA_EMAIL_ADDRESS = ""
UBA_EMAIL_APP_PASSWORD = ""
METRO_EMAIL = ""
LADDITION_EMAIL = ""
LADDITION_PASSWORD = ""
METRO_PASSWORD = ""
ANTHROPIC_API_KEY = ""

# Pinned so a test's result never depends on the developer's own .env.
PRODUCT_FUZZY_MATCH_THRESHOLD = 94

# The timesheet signatures (staff/signing.py): keys, signed PDFs and proof
# files in the tenant's private/ folder (the signing tests give each test a
# TENANTS_ROOT of its own), no passphrase unless a test sets one, and NO
# timestamp server - a test that forgot to inject its own
# (staff.tests.signing_support) is refused « le service d'horodatage ne
# répond pas » rather than reaching DigiCert.
MARGINMATE_SIGNING_PASSPHRASE = ""
STAFF_TIMESTAMP_URLS = []
SITE_URL = ""
# No mail server: « configured » is EMAIL_HOST set, which a test does with
# override_settings - and the test runner's locmem backend keeps every
# message in django.core.mail.outbox.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = ""
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
DEFAULT_FROM_EMAIL = "marginmate@example.invalid"

# Keeps failure output readable when a view raises.
TEMPLATES[0]["OPTIONS"]["debug"] = False
