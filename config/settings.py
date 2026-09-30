"""
Django settings for the MarginMate project.

Production is `manage.py serve` (accounts/management/commands/serve.py):
Waitress on 127.0.0.1, behind a Cloudflare Tunnel, DEBUG forced off, and
these settings driven by the environment (.env) - DEPLOY.md lists the lines.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from .logs import is_the_server, logging_config
from .security import (
    DEVELOPMENT_SECRET_KEY,
    HOW_TO_MAKE_A_KEY,
    env_list,
    secret_key_problem,
)

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# What this process is, read from its command line the way manage.py reads
# it: the production server (`manage.py serve`), the development server
# (`manage.py runserver`), or anything else - a command, a test run, a WSGI
# server loading config.wsgi. Two things differ between them: who writes the
# log file (LOGGING, below) and where /static/ is read from (WhiteNoise).
_SERVER = is_the_server(sys.argv)
_RUNSERVER = sys.argv[1:2] == ["runserver"]


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# OFF unless the environment says otherwise (security audit ANON-2,
# DEPLOY-1, LB-1): with DEBUG on, any visitor could get Django's technical
# pages - the settings with the integrations' logins, the paths, the route
# map - from a forged Host or an unknown address. A developer's .env says
# DJANGO_DEBUG=True; `manage.py serve` forces it off whatever .env says
# (manage.py), and the check accounts.E007 refuses DEBUG on beside a public
# host in ALLOWED_HOSTS - that is a deployed server.
DEBUG = env_bool("DJANGO_DEBUG", False)

# No public fallback (DEPLOY-2, ANON-7). DEBUG off, a missing or weak key -
# the .env.example placeholder, the old fallback, « django-insecure… »,
# fewer than 50 characters - is refused HERE, at load: a WSGI server runs no
# system check. DEBUG on (a developer's machine) and no key: the public
# development key, which accounts.W001 names at every start; a weak key set
# by hand is refused by accounts.E006 wherever DEBUG is off. Never printed.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
_KEY_PROBLEM = secret_key_problem(SECRET_KEY)
if _KEY_PROBLEM and not DEBUG:
    raise ImproperlyConfigured(f"{_KEY_PROBLEM} (DJANGO_DEBUG est désactivé). {HOW_TO_MAKE_A_KEY}")
if not SECRET_KEY.strip():
    SECRET_KEY = DEVELOPMENT_SECRET_KEY

# The names this server answers to: the public one (gestion.<domain>) and
# the machine's own, for the owner's browser on the PC.
ALLOWED_HOSTS = env_list(os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"))
# Origins a POST may come from besides this site's own address
# (« https://gestion.<domain> »). Behind the tunnel the Host header is the
# public name and Waitress gives the request its https (serve.py), so
# Django's same-origin check already passes; this list is the explicit say.
CSRF_TRUSTED_ORIGINS = env_list(os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", ""))

# --- HTTPS (security audit ANON-3, DEPLOY-3) -------------------------------------
# MARGINMATE_HTTPS=1 when the site is reached over HTTPS (the tunnel): the
# session and CSRF cookies are sent over HTTPS only, and browsers are told to
# come back over HTTPS (HSTS) - for an hour at first (MARGINMATE_HSTS_SECONDS
# raises it once everything works), never with includeSubDomains or preload:
# the domain's other names are not this server's to decide. No
# SECURE_SSL_REDIRECT: Cloudflare's « Always Use HTTPS » redirects before the
# tunnel, and here it would send the owner's own http://127.0.0.1:8765 (the
# production server's port, accounts/management/commands/serve.py) to an
# https that does not exist. No SECURE_PROXY_SSL_HEADER either: the one place
# that trusts the proxy's X-Forwarded-Proto and X-Forwarded-For is Waitress
# (`serve`, trusted_proxy 127.0.0.1), which sets the request's scheme and
# REMOTE_ADDR before Django sees it - a header Django would trust by itself
# could be sent by anyone reaching the server another way.
# `manage.py serve` refuses to start without it (accounts.E009).
HTTPS = env_bool("MARGINMATE_HTTPS", False)
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
_HSTS = os.environ.get("MARGINMATE_HSTS_SECONDS", "").strip() or "3600"
if not _HSTS.isascii() or not _HSTS.isdigit():
    raise ImproperlyConfigured(
        f"MARGINMATE_HSTS_SECONDS={_HSTS[:20]!r} : un nombre de secondes est attendu (3600 = une heure)."
    )
SECURE_HSTS_SECONDS = int(_HSTS) if HTTPS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Django's defaults, said here so that a test pins them. The session cookie is
# out of JavaScript's reach; the CSRF cookie is not - htmx reads it
# (base.html).
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
# DENY everywhere; the logged-in file view says SAMEORIGIN itself (the
# invoice's PDF is shown in a frame of this site).
X_FRAME_OPTIONS = "DENY"

# `check --deploy` (and `manage.py serve`, which refuses on any warning) is
# told what this deployment decided on purpose: no SSL redirect here
# (security.W008, see above), HSTS without includeSubDomains (W005) nor
# preload (W021).
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W008", "security.W021"]


INSTALLED_APPS = [
    # django.contrib.admin with the project's site: superusers only
    # (accounts/admin_site.py).
    "accounts.admin_site.MarginMateAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Logins, tenants (one database per bar) and the binding of a thread to
    # one of them: accounts/tenancy.py.
    "accounts",
    "inventory",
    "invoices",
    "recipes",
    "bank",
    "transfer",
    "margins",
    "staff",
    # « Consignes »: the empties handed back, and the slips they are compared
    # with. Achats, the gather, the supplier page and « Données » read its
    # tables too: a tenant needs its migrations (migrate_tenants).
    "returnables",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # /static/ straight from STATIC_ROOT (collectstatic, which `serve` runs at
    # every start), before anything asks for a login: the login page needs
    # its stylesheet. Under runserver or DEBUG it serves from the source
    # folders (WHITENOISE_USE_FINDERS, below STATIC_ROOT).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # The pages' Content-Security-Policy (config/security.py); above
    # XFrameOptionsMiddleware, whose header it reads on the way out.
    "config.security.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every page wants a login (public views carry @login_not_required),
    # then the request runs bound to the user's tenant, rendering included -
    # a public view's never (it binds what it needs, the signing pages their
    # link's tenant).
    "accounts.middleware.LoginRequiredMiddleware",
    "accounts.middleware.TenantMiddleware",
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
                "invoices.context_processors.receipt_review_count",
                "config.navigation.navigation",
            ],
        },
    },
]

# What runserver (and any WSGI server but ours) serves: it refuses what came
# through the Cloudflare Tunnel, which is `manage.py serve`'s alone - serve
# builds its own handler (config/wsgi.py).
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
    # A transaction that has read, then writes after another connection
    # committed a write - a job's heartbeat, every 15 s - fails at once with
    # "database is locked" in SQLite's default (deferred) mode: the timeout
    # above is not even tried, and the invoice being imported was lost.
    # Taking the write lock when the transaction starts waits instead.
    "transaction_mode": "IMMEDIATE",
}

#: The central database's options: the same WAL and wait, a
#: shorter wait - a login must not hang a minute. IMMEDIATE too: a signup
#: reads its invitation then writes, which in the default (deferred) mode
#: fails at once instead of waiting when another login wrote in between.
#: Its writes are small (a session, a login), so every bar taking this one
#: file's write lock stays short.
ACCOUNTS_SQLITE_OPTIONS = {**SQLITE_OPTIONS, "timeout": 20}

# One database per bar (« espace ») under TENANTS_ROOT, the logins in an
# accounts database, a login on every page (accounts/tenancy.py). That is
# the ONLY mode. The old « single » mode - one database, no login, every
# folder from a setting - was what a server got when MARGINMATE_TENANCY was
# missing, so a deploy without that one variable served every page and
# every file to anyone (security audit ANON-1); it was removed on
# 29/09/2026, after the owner's database was adopted into a tenant.
#
# MARGINMATE_TENANCY is therefore no switch any more. Unset or « multi »
# (what the owner's .env still says): ignored. ANY OTHER VALUE - « single »
# above all, or a typo - is REFUSED right here, at load: an installation
# that still expects a database with no login must be told, not silently
# moved, and a WSGI server runs no system check, so only a refusal here
# stops it too. A single-mode database is brought in with
# `manage.py adopt_database` (a one-off).
_OLD_TENANCY_SWITCH = os.environ.get("MARGINMATE_TENANCY", "").strip().lower()
if _OLD_TENANCY_SWITCH not in ("", "multi"):
    raise ImproperlyConfigured(
        f"MARGINMATE_TENANCY={_OLD_TENANCY_SWITCH[:20]!r} : le mode « single » (une seule base, aucune connexion) "
        "n'existe plus. Retirez la ligne MARGINMATE_TENANCY du fichier .env : chaque bar a son espace et chaque "
        "page demande une connexion. Une ancienne base s'adopte avec « manage.py adopt_database »."
    )

# Every tenant's folder: its db.sqlite3, media/, private/, downloads/,
# backups/, staging/, imports/ (accounts/paths.py). Outside the code tree in
# production; never served.
TENANTS_ROOT = Path(os.environ.get("MARGINMATE_TENANTS_ROOT", "").strip() or BASE_DIR / "tenants")

# The server's log, marginmate.log (config/logs.py: errors, refusals,
# warnings; the signing links cut short): MARGINMATE_LOG_DIR, or logs/ beside
# the tenants - ../data/logs/ for the owner. Made at the first line written,
# and written by `manage.py serve` ALONE: every other process logs to its
# console (two processes holding the file stopped its rotation on Windows,
# and records were lost - config/logs.py).
LOG_DIR = Path(os.environ.get("MARGINMATE_LOG_DIR", "").strip() or TENANTS_ROOT.parent / "logs")
LOGGING = logging_config(LOG_DIR, server=_SERVER)

DATABASES = {
    # Unbound, `default` is an EMPTY in-memory database: a business query
    # nobody bound to a tenant fails loudly (« no such table ») instead of
    # landing in somebody's file. A binding points this thread's `default`
    # at the tenant's own file (accounts.tenancy.bound_tenant).
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": SQLITE_OPTIONS,
    },
    # Logins, sessions, the admin's log, content types, tenants,
    # invitations, the signing links' index (accounts/router.py).
    "accounts": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.environ.get("MARGINMATE_ACCOUNTS_DB", "").strip() or BASE_DIR / "accounts.sqlite3"),
        "OPTIONS": ACCOUNTS_SQLITE_OPTIONS,
    },
}

DATABASE_ROUTERS = ["accounts.router.AccountsRouter"]


# An inventory is one formset row per thing counted, and a bar counts
# hundreds of things. Django's default cap of 1000 POSTed fields is reached
# at 332 rows on a new stock take (three fields a row, plus the management
# form) and at 249 when editing one (each saved row also posts its id) - and
# what happens there is the worst possible failure: the request is rejected
# outright with 400 Bad Request, before any view runs, so there is no form to
# re-render and every count the user typed is gone with no way back.
#
# That is not a hypothetical - it is what "I lost my stock count after
# clicking save" was. The cap exists to blunt hash-collision
# DoS on public sites; this is a single-user app on a laptop, so it is set to
# a number no real inventory will reach.
# Covered by inventory/tests/test_stock_take_form.py::BigInventoryTests.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 25_000

# The same trap for files: by default Django refuses a request carrying more
# than 100, again with a bare 400 before any view runs. Scanning a whole
# folder of receipt photos in one go is what the Tickets page is for, and a
# year of them is several hundred.
# Covered by invoices/tests/test_receipt_batches.py::BigFolderUploadTests.
DATA_UPLOAD_MAX_NUMBER_FILES = 2_000


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
# Filled by collectstatic (`manage.py serve` runs it, --clear, at every start)
# and served by WhiteNoise. Plain files, no hashed names: `{% asset %}` adds
# the file's date to its address, and Cloudflare compresses on its side.
STATIC_ROOT = BASE_DIR / "staticfiles"
# Where WhiteNoise reads /static/ from. `manage.py serve`: STATIC_ROOT only,
# indexed once at start (serve sets both off again itself). runserver: the
# SOURCE folders, looked up at each request, WHATEVER DEBUG SAYS. Left to
# WhiteNoise's defaults (DEBUG), a runserver with the deployed .env
# (DJANGO_DEBUG=False, which accounts.E007 demands once the public host is
# listed) got no static handler from Django and served the copy the last
# `serve` made: an edited script came back as its old content under a new
# `?v=` (`{% asset %}` dates the SOURCE), a new one was a 404 (review PROD-4).
WHITENOISE_USE_FINDERS = WHITENOISE_AUTOREFRESH = DEBUG or _RUNSERVER

# No MEDIA_URL / MEDIA_ROOT: stored files go where accounts.paths.media_root()
# says, at every call - the bound tenant's media/ - and are served only by
# the logged-in file view (accounts.views.media), never by a /media/ route.
# Every other folder is the tenant's too (accounts/paths.py: private/,
# downloads/, backups/, staging/, imports/); the settings that named them
# for the old single mode are gone.
STORAGES = {
    "default": {"BACKEND": "accounts.storage.TenantFileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The login page: every page but the public ones sends there.
LOGIN_URL = "/connexion/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/connexion/"

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
# Whichever provider the bar's mailbox is at (IMAP over SSL): Gmail unless
# said otherwise - it used to be written into the fetcher itself.
INVOICE_IMAP_HOST = os.environ.get("INVOICE_IMAP_HOST", "") or "imap.gmail.com"

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

# --- « Personnel »: the monthly electronic signature of the timesheets ------------
# (staff/signing.py, staff/signature_requests.py)

# The signing keys, the frozen and signed PDFs, the drawn signatures and the
# proof files live in each tenant's private/ folder (accounts.paths.private_dir),
# never served. Back it up with the tenant: it holds the internal
# authority's key. Losing it does not make a signed PDF unverifiable - each
# one embeds its certificates - but the next signature would come from a new
# authority. (The old single mode's MARGINMATE_PRIVATE_DIR is ignored.)
#
# Encrypts the private keys on disk (PKCS#8). Unset, they are stored in
# clear and the owner's pages say so: set it before the app goes online.
MARGINMATE_SIGNING_PASSPHRASE = os.environ.get("MARGINMATE_SIGNING_PASSPHRASE", "")
# RFC 3161 timestamp servers (no fee), tried in order: the one piece of evidence
# the employer does not control. When none answers, the signature is refused.
STAFF_TIMESTAMP_URLS = [
    url.strip() for url in os.environ.get("MARGINMATE_TIMESTAMP_URLS", "").split(",") if url.strip()
] or ["http://timestamp.digicert.com", "http://timestamp.sectigo.com"]
# A signed month and its evidence are kept this long after the month ends,
# then deleted by `manage.py staff_purge_signatures` (never run automatically).
STAFF_SIGNATURE_RETENTION_YEARS = 5
# What the links sent to an employee start with (« https://bar.example.fr »);
# blank, the page builds them from the request it answers.
SITE_URL = os.environ.get("MARGINMATE_SITE_URL", "").strip().rstrip("/")
# A post refused for its CSRF token is said in French on the employee's
# signing pages (/personnel/signer/…); everywhere else, Django's own page -
# templates/403_csrf.html, French too and nothing internal (the reason goes to
# the log, django.security.csrf). The other error pages are templates/400,
# 403, 404 and 500.html.
CSRF_FAILURE_VIEW = "staff.public_views.csrf_failure"

# E-mail, optional and off by default: « configured » means EMAIL_HOST is
# set. Gmail: smtp.gmail.com, port 587, TLS, and an APP PASSWORD (not the
# account's own) in EMAIL_HOST_PASSWORD.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "").strip() or 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "").strip() or EMAIL_HOST_USER or "webmaster@localhost"
# A mail server that does not answer must not hold a page for minutes.
EMAIL_TIMEOUT = 20
