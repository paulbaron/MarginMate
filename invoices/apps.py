import os
import sys

from django.apps import AppConfig


def serving_requests(argv, environ) -> bool:
    """Whether this process is the one serving the pages - the dev server's
    child (the autoreloader's parent only watches the files), or the dev
    server run without the autoreloader. Every other Django process (a
    shell, a check, a migration) starts too, and has no gathers of its own."""
    if "runserver" not in argv:
        return False
    return environ.get("RUN_MAIN") == "true" or "--noreload" in argv


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
        if not serving_requests(sys.argv, os.environ):
            return

        import warnings

        from django.db.utils import OperationalError
        from django.utils import timezone

        from .models import ScrapeJob

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                stuck_jobs = list(
                    ScrapeJob.objects.filter(status__in=[ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING])
                )
                for job in stuck_jobs:
                    job.append_log("Interrompu par un redémarrage du serveur.")
                    job.status = ScrapeJob.Status.FAILED
                    job.finished_at = timezone.now()
                    job.save(update_fields=["status", "finished_at"])
        except OperationalError:
            pass  # database/migrations not ready yet (e.g. during `migrate` itself)
