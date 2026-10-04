"""`manage.py serve`: the production server.

    .venv\\Scripts\\python.exe manage.py serve               http://127.0.0.1:8765
    .venv\\Scripts\\python.exe manage.py serve --port 8002
    .venv\\Scripts\\python.exe manage.py serve --verifier    the checks only

start_production.cmd runs it; DEPLOY.md says how the site reaches it (a
Cloudflare Tunnel to http://127.0.0.1:8765, installed by the owner).

**8765, never 8000** (review PROD-1): 8000 is runserver's default port, and
both .claude/launch.json previews start runserver there. With the tunnel on
it, whichever development server held the port was the public site - with
Django's technical pages while the .env still said DEBUG, and with every
visitor at 127.0.0.1 otherwise. No development tool defaults to 8765. The
second lock is config/wsgi.py: what runserver serves refuses a request of
the tunnel; `serve` builds its own handler (`Command.serve`).

A management command rather than a script of its own (config/production.py):
it starts from the settings every other command reads (.env), runs Django's
own checks with the deployment ones, reads the migrations with Django's own
executor and runs collectstatic - each a call away - and it is where the
owner already goes (`manage.py migrate_tenants`). In accounts/ because what
it checks before serving is the tenants': the accounts database, the
template, each tenant's database.

In order:

1. **DEBUG off**, whatever .env says: manage.py sets DJANGO_DEBUG=False
   before the settings load (for `serve` only - a missing or weak key is
   then refused right there), and the command sets settings.DEBUG = False
   again for a call from anywhere else.
2. **The checks**, Django's with the deployment ones (`check --deploy`),
   refused on any WARNING or worse that the settings do not silence: a weak
   SECRET_KEY (accounts.E006), no public host or « * » (E008), cookies not
   HTTPS-only (E009), a bad MARGINMATE_SITE_URL (E010), the bars' data
   inside the code's folder (E011), no signing passphrase (staff.W001), a
   cache the login limiter cannot count in (W002)… each printed with what
   to do.
3. **The migrations**: none left to apply in the accounts database, the
   template new tenants are copied from, or any open tenant - each one
   named, and a missing database too. `serve` never migrates: it tells the
   owner to back up the data folder and run migrate_tenants.
4. **collectstatic --clear** into STATIC_ROOT, which WhiteNoise serves - from
   there only, indexed once (WHITENOISE_USE_FINDERS and _AUTOREFRESH off,
   whatever the settings say: runserver's are on). Cleared first, so
   STATIC_ROOT is an exact copy of the code's static files at every start:
   collectstatic alone skips a file whose collected copy is newer than its
   source, and a release whose files keep older dates - a zip, a copy that
   keeps them, going back to a backup - left the previous scripts served
   beside the new templates (review PROD-3). STATIC_ROOT is only ever filled
   by collectstatic (gitignored). Just before, the expired sessions are
   deleted (`clear_expired_sessions`, Django's clearsessions): nothing else
   ever removes them.
5. **Waitress**, on 127.0.0.1 only (cloudflared, on this machine, is the way
   in: no port is opened on the router), `THREADS` threads, ONE process -
   the tenants' binding is per thread, and the login limiter counts in this
   process's memory (accounts/limiter.py). Its socket is bound EXCLUSIVELY
   (Windows lets a second server bind a port already listened on - a
   forgotten runserver would share the tunnel's requests): a port in use is
   a refusal. `waitress_options` holds the rest:

   * the client's real address and scheme, in ONE place: cloudflared, at
     127.0.0.1, is the only trusted proxy, and from it only X-Forwarded-For
     and X-Forwarded-Proto are read. `trusted_proxy_count` 1 takes the
     RIGHT-MOST X-Forwarded-For entry - the one Cloudflare's edge appends,
     the address that reached it; whatever a visitor wrote before it is
     ignored. REMOTE_ADDR is then the visitor's (the login limiter, the
     signature events' proof file) and wsgi.url_scheme https
     (request.is_secure(): HSTS, CSRF's origin check). A request from
     anywhere else keeps its own address and has those headers REMOVED
     (clear_untrusted_proxy_headers), as have X-Forwarded-Host, -Port, -By
     and Forwarded from any sender. CF-Connecting-IP, which the tunnel also
     forwards, holds the same address as that last entry; Waitress does not
     read it, and nothing else here does.
   * no Server header, no traceback to a visitor (Waitress's default), and
     no access log: a signing link's token is in its path (config/logs.py).
6. **Ctrl+C** stops it: Waitress stops its threads, the socket is closed,
   « Serveur arrêté. »
"""

from __future__ import annotations

import socket
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections
from django.db.migrations.executor import MigrationExecutor

from accounts import paths
from accounts.models import Tenant
from accounts.router import ACCOUNTS_ALIAS
from accounts.tenancy import _bound_database

HOST = "127.0.0.1"
#: The tunnel's service URL is http://127.0.0.1:8765 (DEPLOY.md): a port no
#: development tool uses by default - runserver's is 8000 (the docstring).
DEFAULT_PORT = 8765
THREADS = 8
#: cloudflared, on this machine: the one proxy whose forwarded headers count.
TRUSTED_PROXY = "127.0.0.1"
TRUSTED_PROXY_HEADERS = frozenset({"x-forwarded-for", "x-forwarded-proto"})
#: How many migration names a refusal lists per database.
SHOWN = 5

MIGRATE = (
    "Aucune migration n'est faite par le serveur. Sauvegardez d'abord le dossier des données (MARGINMATE_TENANTS_ROOT "
    "et la base des comptes) et le fichier .env, puis lancez « .venv\\Scripts\\python.exe manage.py migrate_tenants », "
    "puis relancez le serveur."
)


def max_body_bytes() -> int:
    """The largest request body Waitress lets through: the biggest upload a
    page takes - a « Données » archive, 4 GB (transfer/archive.py) - with
    room for its multipart framing. Waitress's default, 1 GB, refused such an
    archive with its own English 413 before Django saw it, and DEPLOY.md
    sends a big archive to http://127.0.0.1:8765, where nothing else caps
    it. Waitress keeps a body over half a megabyte in a temporary file, not
    in memory. Through the tunnel, Cloudflare's own cap is lower still."""
    from transfer.archive import MAX_ARCHIVE_BYTES

    return MAX_ARCHIVE_BYTES + 16 * 1024**2


def waitress_options() -> dict:
    """Waitress's settings, but for the socket (`listening_socket`): the
    module's docstring, point 5."""
    return {
        "max_request_body_size": max_body_bytes(),
        "threads": THREADS,
        "trusted_proxy": TRUSTED_PROXY,
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": set(TRUSTED_PROXY_HEADERS),
        "clear_untrusted_proxy_headers": True,
        "log_untrusted_proxy_headers": False,
        # Not « waitress »: nothing says which server software answers.
        "ident": "MarginMate",
    }


def listening_socket(port: int) -> socket.socket:
    """A socket bound to HOST:port for this server alone
    (SO_EXCLUSIVEADDRUSE on Windows): OSError when the port is in use."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise
    return sock


def check_problems() -> list[str]:
    """Every system check, the deployment ones included, at WARNING or worse
    and not silenced: « [id] message - À faire : hint »."""
    problems = []
    for message in checks.run_checks(include_deployment_checks=True):
        if message.level < checks.WARNING or message.is_silenced():
            continue
        text = f"[{message.id}] {message.msg}"
        if message.hint:
            text += f"\n    À faire : {message.hint}"
        problems.append(text)
    return problems


def _is_memory(name) -> bool:
    name = str(name or "")
    return name == ":memory:" or "mode=memory" in name


def _unapplied(connection) -> list[str]:
    """The migrations of the code not applied to `connection`'s database,
    read with Django's own executor (nothing is written)."""
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def _unapplied_in(database: Path) -> list[str]:
    """`_unapplied` for a database file, made this thread's `default` for
    the reading (`accounts.tenancy._bound_database`, as the template's
    migration does): Django's migration recorder queries through its
    connection's ALIAS, so a connection of its own would read `default`'s
    table instead."""
    with _bound_database(database) as wrapper:
        return _unapplied(wrapper)


def _pending(where: str, names: list[str]) -> str:
    shown = ", ".join(names[:SHOWN]) + (", …" if len(names) > SHOWN else "")
    return f"{where} : {len(names)} migration(s) à appliquer ({shown})."


def migration_problems() -> list[str]:
    """What is not migrated - the accounts database, the template, every
    open tenant - or missing; with, once, what to do about it."""
    problems = []
    accounts_name = (settings.DATABASES.get(ACCOUNTS_ALIAS) or {}).get("NAME")
    if not _is_memory(accounts_name) and not Path(str(accounts_name or "")).is_file():
        # SQLite would create an empty one: looking must create nothing.
        return ["La base des comptes est introuvable (MARGINMATE_ACCOUNTS_DB).", MIGRATE]
    pending = _unapplied(connections[ACCOUNTS_ALIAS])
    if pending:
        problems.append(_pending("La base des comptes", pending))

    template = paths.template_database()
    if not template.is_file():
        problems.append("Le modèle des nouveaux espaces n'existe pas encore (dossier _template).")
    else:
        pending = _unapplied_in(template)
        if pending:
            problems.append(_pending("Le modèle des nouveaux espaces", pending))

    for tenant in Tenant.objects.filter(is_active=True).order_by("pk"):
        label = f"L'espace « {tenant.name} » ({tenant.dir_name})"
        try:
            database = paths.tenant_database(tenant)
        except ImproperlyConfigured:
            problems.append(f"{label} : son nom de dossier est invalide.")
            continue
        if not database.is_file():
            problems.append(
                f"{label} : sa base est introuvable. Restaurez-la depuis une sauvegarde, ou fermez l'espace "
                "(case « actif » décochée dans l'administration)."
            )
            continue
        try:
            pending = _unapplied_in(database)
        except DatabaseError:
            problems.append(f"{label} : sa base ne s'ouvre pas.")
            continue
        if pending:
            problems.append(_pending(label, pending))

    if problems:
        problems.append(MIGRATE)
    return problems


class Command(BaseCommand):
    help = (
        f"Lance le serveur de production (Waitress, http://{HOST}:{DEFAULT_PORT}) après ses vérifications : mode "
        "debug coupé, clé secrète, noms d'hôte, HTTPS, migrations. Ctrl+C pour l'arrêter."
    )
    # Run by the command itself, deployment checks included, once DEBUG is off.
    requires_system_checks = ()
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", type=int, default=DEFAULT_PORT, help=f"Le port, sur {HOST} ({DEFAULT_PORT} par défaut)."
        )
        parser.add_argument(
            "--verifier",
            dest="check_only",
            action="store_true",
            help="Faire les vérifications seulement, sans lancer le serveur.",
        )

    def handle(self, *args, port=DEFAULT_PORT, check_only=False, **options):
        if not 1 <= port <= 65535:
            raise CommandError("Le port est un nombre entre 1 et 65535.")
        was_on = settings.DEBUG
        settings.DEBUG = False
        # /static/ from STATIC_ROOT alone, indexed once: the settings turn the
        # source folders on for runserver (and DEBUG). Read by WhiteNoise when
        # the handler is built, in `serve`.
        settings.WHITENOISE_USE_FINDERS = settings.WHITENOISE_AUTOREFRESH = False
        self.stdout.write("MarginMate - serveur de production")
        if was_on:
            self.stdout.write("Mode debug : demandé, et COUPÉ - le serveur de production ne l'active jamais.")
        else:
            self.stdout.write("Mode debug : coupé.")

        self.stdout.write("Vérifications…")
        problems = check_problems()
        problems += migration_problems()
        if problems:
            for problem in problems:
                self.stderr.write(f"- {problem}")
            raise CommandError(f"Le serveur ne démarre pas : {len(problems)} point(s) ci-dessus.")
        self.stdout.write("Vérifications : tout est en ordre.")
        if check_only:
            return

        try:
            sock = listening_socket(port)
        except OSError:
            raise CommandError(
                f"Le port {port} est déjà pris sur cette machine : un autre serveur y tourne (une autre fenêtre "
                "start_production.cmd, un runserver lancé sur ce port ?). Arrêtez-le, ou choisissez un autre port "
                "avec --port."
            ) from None
        try:
            self.clear_expired_sessions()
            self.stdout.write("Fichiers statiques…")
            # --clear: an exact copy of the code's, whatever the files' dates
            # (the module's docstring, point 4).
            call_command("collectstatic", interactive=False, verbosity=0, clear=True)
        except BaseException:
            sock.close()
            raise
        self.serve(sock, port)

    def clear_expired_sessions(self) -> None:
        """Django's `clearsessions`, at each start: nothing else removes an
        expired session, and a visitor of a signing link that keeps no cookie
        leaves one per post (staff/public_views.py). Only expired ones go:
        nobody is logged out. A failure is said and serving goes on."""
        engine = import_module(settings.SESSION_ENGINE)
        try:
            engine.SessionStore.clear_expired()
        except DatabaseError as error:
            self.stderr.write(
                f"Sessions expirées : non effacées ({type(error).__name__}), le serveur démarre quand même."
            )
            return
        self.stdout.write("Sessions expirées effacées.")

    def serve(self, sock: socket.socket, port: int) -> None:
        """Waitress on `sock` until Ctrl+C; the socket is closed on the way
        out, whatever happens.

        The application is a handler of its own, built now - never
        config.wsgi's, which refuses the tunnel's requests (they are this
        server's alone) and whose middleware would be built with whatever
        the settings said when it was first imported."""
        from django.core.handlers.wsgi import WSGIHandler
        from waitress import create_server

        server = None
        try:
            application = WSGIHandler()
            server = create_server(application, sockets=[sock], **waitress_options())
            log_file = Path(getattr(settings, "LOG_DIR", "")) / "marginmate.log"
            # Only the tunnel's port is called its address: `--port` serves
            # somewhere the tunnel (DEPLOY.md, section 6) does not point.
            where = (
                "c'est l'adresse du tunnel Cloudflare."
                if port == DEFAULT_PORT
                else f"le tunnel Cloudflare vise le port {DEFAULT_PORT}, pas celui-ci."
            )
            self.stdout.write(f"En ligne sur http://{HOST}:{port}/ ({THREADS} fils) : {where}")
            self.stdout.write(f"Journal des erreurs : {log_file}")
            self.stdout.write("Ctrl+C pour arrêter.")
            try:
                # Returns on Ctrl+C: Waitress stops its threads itself.
                server.run()
            except KeyboardInterrupt:
                server.task_dispatcher.shutdown()
        except KeyboardInterrupt:
            pass
        finally:
            if server is not None:
                server.close()
            sock.close()
        self.stdout.write("Serveur arrêté.")
