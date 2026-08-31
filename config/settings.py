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


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
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
UBA_EMAIL_ADDRESS = os.environ.get("UBA_EMAIL_ADDRESS", "")
UBA_EMAIL_APP_PASSWORD = os.environ.get("UBA_EMAIL_APP_PASSWORD", "")

# Used only for the last-resort LLM invoice parsing fallback.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Local LLM (Ollama) used for the "Suggérer avec l'IA" stock-matching feature -
# free, runs on this machine, no API key needed. Requires Ollama running
# (https://ollama.com) with the model pulled: `ollama pull qwen3:8b`.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

# Confidence threshold (0-100) above which a fuzzy product-name match is treated
# as "the same product" instead of being sent to the review queue.
PRODUCT_FUZZY_MATCH_THRESHOLD = int(os.environ.get("PRODUCT_FUZZY_MATCH_THRESHOLD", "92"))

# Run Selenium in headless mode (should stay True for background/server use).
SCRAPER_HEADLESS = env_bool("SCRAPER_HEADLESS", True)

SCRAPE_DOWNLOAD_DIR = BASE_DIR / "scraped_invoices"
