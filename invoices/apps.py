import logging
import os
import sys
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)

#: How long the serving process lets pass before it reaps: two of a
#: gather's heartbeats (tasks.HEARTBEAT_SECONDS, 15 s - not imported here,
#: which would load every scraper as the apps load; a test holds the two
#: together) and a margin. A gather another live process runs - `serve`,
#: while a debug runserver starts on the same data - has beaten by then
#: since this process started; one whose process died has not.
REAP_DELAY_SECONDS = 2 * 15 + 5


def serving_requests(argv, environ) -> bool:
    """Whether this process is the one serving the pages - the dev server's
    child (the autoreloader's parent only watches the files), or the dev
    server run without the autoreloader. Every other Django process (a
    shell, a check, a migration) starts too, and has no gathers of its own."""
    if "runserver" not in argv:
        return False
    return environ.get("RUN_MAIN") == "true" or "--noreload" in argv


def reap_interrupted_gathers(started) -> None:
    """Mark failed the gathers a server restart interrupted - those not
    heard from since `started`, this process's start - in every espace,
    each bound in turn, and never in the unbound `default`: that is
    nobody's database (an empty one in production), and before the espaces
    it was the owner's file whoever the jobs belonged to. An espace whose
    database cannot be bound (its file missing) is stepped over: it has
    nothing to reap, and the others still do."""
    from django.core.exceptions import ImproperlyConfigured
    from django.db.utils import DatabaseError

    from accounts.models import Tenant
    from accounts.tenancy import TenancyError, bound_tenant

    try:
        espaces = list(Tenant.objects.filter(is_active=True))
    except DatabaseError:
        return  # the accounts database is not migrated yet
    for espace in espaces:
        try:
            with bound_tenant(espace):
                _reap_here(started)
        except (TenancyError, ImproperlyConfigured):
            continue


def _reap_here(started) -> None:
    """Every job still PENDING or RUNNING whose last sign of life - its
    heartbeat, else its start - is older than `started`. The status is
    changed only if that still holds as it is written: a beat landing in
    between says the job is alive."""
    from django.db.models import Q
    from django.db.utils import OperationalError
    from django.utils import timezone

    from .models import ScrapeJob

    active = [ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING]
    unheard = Q(last_heartbeat__lt=started) | Q(last_heartbeat__isnull=True, started_at__lt=started)
    try:
        for job in list(ScrapeJob.objects.filter(unheard, status__in=active)):
            reaped = ScrapeJob.objects.filter(unheard, pk=job.pk, status__in=active).update(
                status=ScrapeJob.Status.FAILED, finished_at=timezone.now()
            )
            if reaped:
                job.refresh_from_db()
                job.append_log("Interrompu par un redémarrage du serveur.")
    except OperationalError:
        pass  # database/migrations not ready yet (e.g. during `migrate` itself)


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def _reap_after_delay(started, origin: int) -> None:
    """The startup reaper's thread: wait REAP_DELAY_SECONDS, reap what was
    not heard from since `started`, and close this thread's connections -
    unless it runs in the thread that made it (`origin`: a test running the
    target in place), whose connections are not its to close."""
    try:
        _wait(REAP_DELAY_SECONDS)
        reap_interrupted_gathers(started)
    except Exception:  # noqa: BLE001 - a dead job is still reaped where its page polls it
        logger.exception("Le nettoyage des récupérations interrompues au démarrage a échoué.")
    finally:
        if threading.get_ident() != origin:
            from django.db import connections

            connections.close_all()


class InvoicesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'invoices'

    def ready(self):
        # Gather jobs run in a plain background thread (see tasks.py), which
        # cannot survive a server restart. Any job still marked PENDING/RUNNING
        # when the server starts was interrupted (dev server autoreload,
        # crash, ...) and would otherwise block "Gather new invoices" until
        # its heartbeat went stale. Only the serving process does it: a
        # `manage.py shell` opened to look at a gather marked it failed seven
        # seconds in while its thread went on to sign in to Metro, and the
        # gather was run again at once (invoices/tests/test_startup_reaper.py).
        # Under the production server (`manage.py serve`, Waitress) nothing
        # runs here: its apps load before it knows whether it will serve
        # (`serve --verifier`, or a second `serve` refused its port, would
        # reap the live server's gathers). A dead job is reaped where its
        # page polls it (ScrapeJob.reap_stale).
        #
        # Nor is a gather another LIVE process runs reaped (security review
        # PROD-2): a runserver started beside `serve` on the same data - the
        # debug recipe of DEPLOY.md, or a preview refused its port - marked
        # serve's running gathers FAILED as its apps loaded, the Gather
        # button came back and a second gather could start beside the first.
        # So the reaping waits REAP_DELAY_SECONDS in a thread of its own - it
        # holds nothing up - and takes only the jobs not heard from since
        # this process started: a live gather beats every 15 s, a dead one
        # never again.
        if not serving_requests(sys.argv, os.environ):
            return
        from django.utils import timezone

        threading.Thread(
            target=_reap_after_delay,
            args=(timezone.now(), threading.get_ident()),
            name="marginmate-startup-reaper",
            daemon=True,
        ).start()
