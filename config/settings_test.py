"""Settings used by the test suite: `manage.py test --settings=config.settings_test`.

Two jobs beyond speed. First, it makes it *impossible* for a test to reach a
real service by accident: every credential is blanked, so the IMAP fetcher,
the Metro scraper and the LLM parser all refuse to start rather than dialling
out with the developer's real .env values. Second, it pins anything a test's
outcome could otherwise inherit from the local .env (notably the fuzzy-match
threshold), so a passing suite means the same thing on every machine.
"""

import tempfile

from .settings import *  # noqa: F401,F403

# In-memory DB: never touches db.sqlite3, and no file to clean up.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

DEBUG = False

# Anything a test writes lands in a temp dir, not the real media/ or
# scraped_invoices/ folders.
_TEST_TMP = tempfile.mkdtemp(prefix="marginmate-tests-")
MEDIA_ROOT = _TEST_TMP
SCRAPE_DOWNLOAD_DIR = _TEST_TMP

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

# Keeps failure output readable when a view raises.
TEMPLATES[0]["OPTIONS"]["debug"] = False  # noqa: F405
