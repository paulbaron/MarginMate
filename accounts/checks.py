"""System checks of the tenants and of the server: `manage.py check`,
every runserver, and `manage.py serve`, which also runs the deployment ones
(`check --deploy`) and refuses to start on any warning.

Always run - there is no mode left in which they would have nothing to say
(the single mode, and its accounts.E001 « unknown mode », were removed on
29/09/2026). They refuse to start without somewhere to keep the tenants
(TENANTS_ROOT), without the accounts database, or with a `default` that is
a real file - unbound, `default` must be an EMPTY in-memory database, so a
query nobody bound fails loudly instead of landing in that file
(E002-E004). They refuse more than one open tenant using the server's own
accounts (`one_owner_tenant`, E005), and warn about a cache the login
limiter cannot count in (`limiter_cache`, W002).

The server's own (security audit DEPLOY-1, DEPLOY-2, ANON-7, ANON-3):

* `secret_key`: a weak SECRET_KEY (config/security.py says what « weak »
  is) - sessions, CSRF, the messages cookie and the signing codes' HMAC
  rest on it. A warning with DEBUG on (a developer's machine, W001), an
  Error with DEBUG off (E006). The key is never printed.
* `debug_on_a_public_host`: DEBUG on while ALLOWED_HOSTS names a host that
  is not this machine's - a deployed server showing Django's debug pages to
  anybody (E007). No opt-out: a developer's machine lists local names only.
* deployment only (`check --deploy`, `serve`): ALLOWED_HOSTS with a public
  name and no « * » (E008), the cookies HTTPS-only (E009), the signing
  links' MARGINMATE_SITE_URL in https and allowed (E010), and every bar's
  data - the tenants, the accounts database - outside the code's folder
  (E011): left at their defaults they sit beside manage.py, where updating
  or cleaning the code goes.

A WSGI server runs no system check: config/settings.py refuses an old
MARGINMATE_TENANCY value and, DEBUG off, a missing or weak key itself, at
load.
"""

from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.db import DatabaseError
from django.http.request import validate_host

from config.security import (
    DEVELOPMENT_SECRET_KEY,
    HOW_TO_MAKE_A_KEY,
    public_hosts,
    secret_key_problem,
)

from .router import ACCOUNTS_ALIAS

#: config/settings.py's fallback when DJANGO_SECRET_KEY is unset and DEBUG
#: is on (a developer's machine only).
PUBLIC_SECRET_KEY = DEVELOPMENT_SECRET_KEY


def _is_memory(name) -> bool:
    name = str(name or "")
    return name == ":memory:" or "mode=memory" in name


@register()
def tenancy_settings(app_configs=None, **kwargs):
    issues = []
    root = getattr(settings, "TENANTS_ROOT", None)
    if not root or not str(root).strip():
        issues.append(
            Error(
                "TENANTS_ROOT is not set.",
                hint="Every espace's database and files are kept under it (MARGINMATE_TENANTS_ROOT).",
                id="accounts.E002",
            )
        )
    databases = settings.DATABASES
    accounts = databases.get(ACCOUNTS_ALIAS) or {}
    if not str(accounts.get("NAME") or "").strip():
        issues.append(
            Error(
                "The 'accounts' database is not configured.",
                hint="The logins, sessions and espaces are kept in it (MARGINMATE_ACCOUNTS_DB).",
                id="accounts.E003",
            )
        )
    if not _is_memory((databases.get("default") or {}).get("NAME")):
        issues.append(
            Error(
                "The 'default' database must be ':memory:'.",
                hint="Unbound, a business query must fail loudly, not write into a real file.",
                id="accounts.E004",
            )
        )
    return issues


@register(Tags.security)
def secret_key(app_configs=None, **kwargs):
    """accounts.W001 (DEBUG on) / accounts.E006 (DEBUG off): a weak
    SECRET_KEY - missing, public, « django-insecure… », too short, too few
    distinct characters. Says what is wrong, never the key."""
    problem = secret_key_problem(getattr(settings, "SECRET_KEY", ""))
    if not problem:
        return []
    if settings.DEBUG:
        return [
            Warning(
                f"{problem} : acceptable sur un poste de développement seulement, jamais en ligne.",
                hint=HOW_TO_MAKE_A_KEY,
                id="accounts.W001",
            )
        ]
    return [Error(f"{problem}.", hint=HOW_TO_MAKE_A_KEY, id="accounts.E006")]


@register(Tags.security)
def debug_on_a_public_host(app_configs=None, **kwargs):
    """accounts.E007: DEBUG on while ALLOWED_HOSTS names a host that is not
    this machine's own - a server online, whose every visitor could get
    Django's debug pages (the settings, the integrations' logins, the paths,
    the route map)."""
    if not settings.DEBUG:
        return []
    public = public_hosts(getattr(settings, "ALLOWED_HOSTS", ()))
    if not public:
        return []
    return [
        Error(
            f"Le mode debug est activé alors que DJANGO_ALLOWED_HOSTS nomme un hôte public ({', '.join(public)}) : "
            "c'est un serveur en ligne, et le mode debug montre à n'importe qui les réglages, les chemins et la "
            "liste des adresses du site.",
            hint=(
                "Mettez DJANGO_DEBUG=False dans .env (manage.py serve le coupe de toute façon). Pour un essai en "
                "mode debug sur ce PC seulement : DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1."
            ),
            id="accounts.E007",
        )
    ]


@register(Tags.security, deploy=True)
def allowed_hosts_online(app_configs=None, **kwargs):
    """accounts.E008 (deployment): ALLOWED_HOSTS names the public host, and
    not « * » (any Host header accepted: the links a page builds could
    point anywhere)."""
    hosts = [str(host).strip() for host in getattr(settings, "ALLOWED_HOSTS", ())]
    hint = (
        "Dans .env : DJANGO_ALLOWED_HOSTS=<le nom public du site>,localhost,127.0.0.1 "
        "(par exemple gestion.example.com,localhost,127.0.0.1)."
    )
    if "*" in hosts:
        return [
            Error(
                "DJANGO_ALLOWED_HOSTS contient « * » : le serveur accepterait n'importe quel nom d'hôte.",
                hint=hint,
                id="accounts.E008",
            )
        ]
    if not public_hosts(hosts):
        return [
            Error(
                "DJANGO_ALLOWED_HOSTS ne nomme aucun hôte public : les pages demandées par le tunnel, sous le nom "
                "public du site, seraient toutes refusées (erreur 400).",
                hint=hint,
                id="accounts.E008",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def https_cookies_online(app_configs=None, **kwargs):
    """accounts.E009 (deployment): the session and CSRF cookies go over
    HTTPS only (MARGINMATE_HTTPS=1): a session sent once over plain HTTP - a
    bar's public Wi-Fi - is somebody else's to use."""
    if getattr(settings, "SESSION_COOKIE_SECURE", False) and getattr(settings, "CSRF_COOKIE_SECURE", False):
        return []
    return [
        Error(
            "Les cookies de session et CSRF ne sont pas réservés à HTTPS : sur un Wi-Fi public, la session d'un "
            "bar pourrait être volée.",
            hint="Mettez MARGINMATE_HTTPS=1 dans .env : le site est servi en HTTPS par Cloudflare.",
            id="accounts.E009",
        )
    ]


@register(Tags.security, deploy=True)
def site_url_online(app_configs=None, **kwargs):
    """accounts.E010 (deployment): MARGINMATE_SITE_URL, when set, is an
    https address whose host is allowed - the signing links e-mailed to the
    employees start with it, and so do their invitations (accounts/members.py)."""
    url = str(getattr(settings, "SITE_URL", "") or "").strip()
    if not url:
        return []
    parts = urlsplit(url)
    hint = "Dans .env : MARGINMATE_SITE_URL=https://<le nom public du site>, nom repris dans DJANGO_ALLOWED_HOSTS."
    if parts.scheme != "https" or not parts.hostname:
        return [
            Error(
                "MARGINMATE_SITE_URL n'est pas une adresse https:// : les liens de signature et d'invitation "
                "envoyés aux salariés partiraient en clair, ou nulle part.",
                hint=hint,
                id="accounts.E010",
            )
        ]
    if not validate_host(parts.hostname, getattr(settings, "ALLOWED_HOSTS", ())):
        return [
            Error(
                "Le nom de MARGINMATE_SITE_URL n'est pas dans DJANGO_ALLOWED_HOSTS : les liens de signature et "
                "d'invitation envoyés aux salariés mèneraient à une erreur 400.",
                hint=hint,
                id="accounts.E010",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def data_outside_the_code(app_configs=None, **kwargs):
    """accounts.E011 (deployment): TENANTS_ROOT and the accounts database are
    not inside the code's folder (BASE_DIR) - their defaults, for a
    developer's machine."""
    base = Path(settings.BASE_DIR).resolve()
    inside = []
    for variable, value in (
        ("MARGINMATE_TENANTS_ROOT", getattr(settings, "TENANTS_ROOT", "")),
        ("MARGINMATE_ACCOUNTS_DB", (settings.DATABASES.get(ACCOUNTS_ALIAS) or {}).get("NAME")),
    ):
        if not str(value or "").strip() or _is_memory(value):
            continue
        path = Path(value).resolve()
        if path == base or base in path.parents:
            inside.append(variable)
    if not inside:
        return []
    return [
        Error(
            f"Les données des bars sont dans le dossier du code ({', '.join(inside)}) : une mise à jour ou un "
            "nettoyage du code pourrait les emporter.",
            hint=(
                "Dans .env : MARGINMATE_TENANTS_ROOT et MARGINMATE_ACCOUNTS_DB vers un dossier à part (celui des "
                "données, à côté du dossier du code), qui se sauvegarde seul."
            ),
            id="accounts.E011",
        )
    ]


#: The caches the login limiter cannot count in (accounts/limiter.py): its
#: window rests on `add` then an `incr` that is atomic and keeps the key's
#: expiry. The file and database caches re-set the key with their default
#: TIMEOUT at every `incr` (the window shrinks to 5 minutes from the latest
#: failure) and are not atomic across processes; the dummy cache keeps
#: nothing, so nothing is ever held back.
LIMITER_UNFIT_CACHES = (
    "django.core.cache.backends.filebased.FileBasedCache",
    "django.core.cache.backends.db.DatabaseCache",
    "django.core.cache.backends.dummy.DummyCache",
)


@register()
def limiter_cache(app_configs=None, **kwargs):
    """accounts.W002: the default cache is one the login limiter cannot
    count in (`LIMITER_UNFIT_CACHES`). Django's default, LocMemCache, is
    right for the production server: `manage.py serve` is ONE process
    (Waitress, threads), whose memory every request shares."""
    backend = str((getattr(settings, "CACHES", {}).get("default") or {}).get("BACKEND") or "")
    if backend not in LIMITER_UNFIT_CACHES:
        return []
    return [
        Warning(
            f"The login limiter counts its attempts in {backend}, which cannot hold its window.",
            hint=(
                "Its counts need an incr that is atomic and keeps the key's expiry: the file and database caches "
                "re-set the key with their default TIMEOUT at every failure and are not atomic, the dummy cache "
                "keeps nothing. Leave CACHES unset: Django's in-memory cache (LocMemCache) counts right for "
                "manage.py serve, which is one process (Waitress, several threads). Redis "
                "(django.core.cache.backends.redis.RedisCache) or Memcached only for several processes."
            ),
            id="accounts.W002",
        )
    ]


@register()
def one_owner_tenant(app_configs=None, **kwargs):
    """At most ONE open tenant uses the server's own accounts
    (`Tenant.uses_server_integrations`, accounts.tenancy.
    server_accounts_allowed: Metro, the supplier portals, and the .env's
    mailbox and L'Addition values). Every other tenant runs the mailbox and
    L'Addition with its own « Identifiants » - this rule is about the
    server's, which stay one owner's.

    They are one set of accounts with one state. Metro's pause - 24 hours
    between two sign-ins, a week after a refusal - lives on the METRO row of
    the tenant that signs in: two such tenants would each keep a pause of
    their own and sign in to the one account twice as often, which is how
    its firewall blocks the owner. adopt_database refuses to make a second
    one and the admin shows the box read-only; this is for one set by hand.

    Reads the accounts database only when its file is there: SQLite creates
    the file it is asked to open, and looking must create nothing (before
    `migrate_tenants` made it, E003's settings are all there is to check).
    An accounts database not migrated yet has nothing to say either."""
    name = str((settings.DATABASES.get(ACCOUNTS_ALIAS) or {}).get("NAME") or "").strip()
    if not name or (not _is_memory(name) and not Path(name).is_file()):
        return []
    from .models import Tenant

    try:
        owners = list(
            Tenant.objects.filter(is_active=True, uses_server_integrations=True)
            .order_by("pk")
            .values_list("name", "dir_name")
        )
    except DatabaseError:
        return []
    if len(owners) <= 1:
        return []
    named = ", ".join(f"« {tenant_name} » ({dir_name})" for tenant_name, dir_name in owners)
    return [
        Error(
            f"{len(owners)} espaces ouverts utilisent les accès du serveur : {named}.",
            hint=(
                "Un seul le peut - un seul compte Metro, une seule pause. Retirez « utilise les accès du "
                "serveur » aux autres, ou fermez-les (actif décoché) : manage.py shell, puis "
                'Tenant.objects.filter(dir_name="<dossier>").update(uses_server_integrations=False).'
            ),
            id="accounts.E005",
        )
    ]
