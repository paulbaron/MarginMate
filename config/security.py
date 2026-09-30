"""What the production settings and checks rest on (config/settings.py,
accounts/checks.py, `manage.py serve`): the SECRET_KEY's rules, which hosts
are local, and the Content-Security-Policy of the application's pages.

Imported by config/settings.py while it loads: nothing here may import a
module that reads Django's settings.

**The SECRET_KEY** (security audit DEPLOY-2, ANON-7). Sessions, the CSRF
token, the messages cookie, the inventory's undo token, the signature
deletion's confirmation and the HMAC of the employees' one-time codes all
rest on it. A key anybody can read - the old settings fallback, the
.env.example placeholder that the owner's .env once held, anything starting
« django-insecure » - lets a stranger sign those values: a forged messages
cookie rendered a script on the login page. `secret_key_problem` says what is
wrong with a key, in French, WITHOUT the key nor its length: a refusal is
printed on a console and may be pasted anywhere.

**The Content-Security-Policy** (DEPLOY-6), `ContentSecurityPolicyMiddleware`:
every HTML page of the application gets `APP_POLICY` unless its view set a
policy of its own - the employee's signing pages keep their stricter one
(staff/public_views.py), a downloaded file its « sandbox »
(accounts/views.py). `frame-ancestors` follows what the page says about
being framed: X-Frame-Options SAMEORIGIN (the logged-in file view,
@xframe_options_sameorigin) gives 'self', anything else 'none' - a
site-wide 'none' would override the SAMEORIGIN of a framed page in current
browsers. Only HTML is given the policy: a PDF shown in the invoice's frame
is drawn by the browser's own viewer, which a policy on the PDF itself can
blank out.

script-src and style-src keep 'unsafe-inline' for now, and script-src
'unsafe-eval' - with evidence, which `tests/test_security_headers.py` greps
again at every run and which fails the day it is gone, so the policy
tightens with the templates:

* inline scripts (`<script>` with no src, not a JSON island): base.html,
  inventory/stock_list.html, stock_take_detail.html, stock_take_form.html,
  invoices/document_review.html, manual_invoice_form.html, purchases.html,
  recipes/recipe_detail.html, recipe_form.html, sale_document_form.html;
* inline event handlers (`onsubmit="return confirm(…)"`, `onchange`,
  `onclick`) in inventory/stock_list.html, stock_take_detail.html,
  stock_type_form.html, _catalogue.html, _stock_type_movements.html,
  recipes/recipe_detail.html, _tab_link.html, _tab_sales.html;
* `style="…"` attributes (75 in the templates, 29/09/2026), a `<style>` block
  (transfer/page.html) and the one htmx inserts for its indicators;
* 'unsafe-eval': htmx turns a trigger's filter into code with `Function()` -
  invoices/_receipt_batch_status.html's `every 1s [!shopChoiceInUse()]`.
  Refused, htmx drops the filter and the polling replaces the shop being
  chosen every second.

What the policy already holds with them: no plugin (object-src), no <base>
rewriting every link (base-uri), no form posting anywhere but here
(form-action), no framing of a page by another site (frame-ancestors), and
no request, image or frame to another origin (default-src 'self'; img-src
adds data: - the drawn signature - and blob: - the Consignes photos'
previews).
"""

from __future__ import annotations

#: config/settings.py's fallback, when DEBUG is on and DJANGO_SECRET_KEY is
#: not set: a developer's machine only - public, like every value below.
DEVELOPMENT_SECRET_KEY = "django-insecure-dev-key-change-me"

#: Keys anybody can read: the development fallback, and the .env.example
#: placeholder (the owner's real .env held it until 29/09/2026).
PUBLIC_SECRET_KEYS = frozenset({DEVELOPMENT_SECRET_KEY, "change-me-to-a-random-secret"})

#: Django's own measure of a strong key (security.W009).
SECRET_KEY_MIN_LENGTH = 50
SECRET_KEY_MIN_UNIQUE_CHARACTERS = 5

#: How to make one - said in every refusal about the key.
HOW_TO_MAKE_A_KEY = (
    'Générez-en une : .venv\\Scripts\\python.exe -c "from django.core.management.utils import '
    'get_random_secret_key; print(get_random_secret_key())", puis mettez-la dans .env '
    "(DJANGO_SECRET_KEY=…). Changer de clé déconnecte tout le monde et annule les codes de signature en cours."
)


def secret_key_problem(key) -> str:
    """What is wrong with `key`, in French - "" when nothing is. Never
    names the key nor its length."""
    key = str(key or "")
    if not key.strip():
        return "DJANGO_SECRET_KEY n'est pas définie"
    if key in PUBLIC_SECRET_KEYS:
        return "la clé secrète est une valeur publique (celle de l'exemple, ou l'ancienne valeur de secours)"
    if key.startswith("django-insecure"):
        return "la clé secrète commence par « django-insecure » : c'est une clé de développement"
    if len(key) < SECRET_KEY_MIN_LENGTH:
        return f"la clé secrète fait moins de {SECRET_KEY_MIN_LENGTH} caractères"
    if len(set(key)) < SECRET_KEY_MIN_UNIQUE_CHARACTERS:
        return f"la clé secrète a moins de {SECRET_KEY_MIN_UNIQUE_CHARACTERS} caractères différents"
    return ""


#: The names that only ever reach this machine. "testserver" is Django's
#: test client's.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1", "testserver"})


def is_local_host(host) -> bool:
    host = str(host or "").strip().lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def public_hosts(allowed_hosts) -> list[str]:
    """The entries of ALLOWED_HOSTS that are not this machine's own ("*"
    included: it names every host)."""
    return [str(host) for host in allowed_hosts or () if not is_local_host(host)]


def env_list(value) -> list[str]:
    """A comma-separated environment value as a list, blanks dropped."""
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


# -- Content-Security-Policy ---------------------------------------------------------------------

#: Every directive but frame-ancestors, which follows X-Frame-Options.
APP_POLICY_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
)

APP_POLICY = "; ".join((*APP_POLICY_DIRECTIVES, "frame-ancestors 'none'"))
#: A page that may be framed by this site (X-Frame-Options SAMEORIGIN).
SAME_ORIGIN_POLICY = "; ".join((*APP_POLICY_DIRECTIVES, "frame-ancestors 'self'"))
#: A page that says nothing about framing (@xframe_options_exempt).
UNFRAMED_POLICY = "; ".join(APP_POLICY_DIRECTIVES)

HEADER = "Content-Security-Policy"


def policy_for(response) -> str:
    """The policy an HTML response gets: frame-ancestors from its
    X-Frame-Options."""
    if getattr(response, "xframe_options_exempt", False):
        return UNFRAMED_POLICY
    if str(response.get("X-Frame-Options", "")).strip().upper() == "SAMEORIGIN":
        return SAME_ORIGIN_POLICY
    return APP_POLICY


class ContentSecurityPolicyMiddleware:
    """Sets `policy_for(response)` on every HTML response that has no
    Content-Security-Policy of its own (the module's docstring). Listed
    above XFrameOptionsMiddleware in settings.MIDDLEWARE, so that the
    X-Frame-Options it reads is already set on the way out."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if HEADER in response:
            return response
        content_type = str(response.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if content_type == "text/html":
            response[HEADER] = policy_for(response)
        return response
