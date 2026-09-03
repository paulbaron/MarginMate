"""
Django settings for the MarginMate project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-key-change-me")

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "inventory",
    "invoices",
    "recipes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "inventory.context_processors.review_count",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


#: How the app shares one SQLite file between a background import that
#: writes for minutes and a status page that polls once a second.
SQLITE_OPTIONS = {
    # Background imports write for a while, and meanwhile the status page
    # polls this same database once a second. SQLite locks the whole file to
    # write, so the two contend - and with the default 5-second timeout a
    # three-year sales import died outright with "database is locked"
    # partway through.
    "timeout": 60,
    # WAL lets readers carry on while a writer holds the lock, which is
    # exactly the shape here: one writer, one poller. Set per connection but
    # persisted in the database file itself.
    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": SQLITE_OPTIONS,
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True


STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/admin/login/"

# --- MarginMate specific settings -------------------------------------------------

# Credentials for the invoice scrapers. Never hardcode these - set them in .env.
METRO_EMAIL = os.environ.get("METRO_EMAIL", "")
METRO_PASSWORD = os.environ.get("METRO_PASSWORD", "")
# Shared inbox every email-based InvoiceType is searched against (see
# invoices/scrapers/generic_email.py) - formerly UBA-only env var names,
# kept as a fallback so an existing .env keeps working without editing.
UBA_EMAIL_ADDRESS = os.environ.get("UBA_EMAIL_ADDRESS", "")
UBA_EMAIL_APP_PASSWORD = os.environ.get("UBA_EMAIL_APP_PASSWORD", "")
INVOICE_EMAIL_ADDRESS = os.environ.get("INVOICE_EMAIL_ADDRESS", "") or UBA_EMAIL_ADDRESS
INVOICE_EMAIL_APP_PASSWORD = os.environ.get("INVOICE_EMAIL_APP_PASSWORD", "") or UBA_EMAIL_APP_PASSWORD

# Credentials for the L'Addition till, used to download the "Z digital"
# sales report (see recipes/pos/laddition.py). Same rule: .env only.
LADDITION_EMAIL = os.environ.get("LADDITION_EMAIL", "")
LADDITION_PASSWORD = os.environ.get("LADDITION_PASSWORD", "")

# Used only for the last-resort LLM invoice parsing fallback.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Confidence threshold (0-100) above which a fuzzy product-name match is treated
# as "the same product" instead of being sent to the review queue.
#
# Only ever compared between names that already have identical numbers (see
# inventory/matching.py::numeric_signature), so all this score still has to
# absorb is non-numeric drift - spacing, punctuation, word order, a short
# supplier suffix - which measures 94.7-100 on the real catalogue, while the
# closest genuinely-different pair left ("COCA COLA" vs "COCA COLA ZERO")
# scores 92.8. Raised from 92 to sit inside that gap. Erring high is the safe
# direction: too strict just means a duplicate product in the review queue,
# where it's visible, while too loose merges silently and corrupts a
# product's price history with another product's costs.
PRODUCT_FUZZY_MATCH_THRESHOLD = int(os.environ.get("PRODUCT_FUZZY_MATCH_THRESHOLD", "94"))

# Run Selenium in headless mode (should stay True for background/server use).
SCRAPER_HEADLESS = env_bool("SCRAPER_HEADLESS", True)

SCRAPE_DOWNLOAD_DIR = BASE_DIR / "scraped_invoices"
