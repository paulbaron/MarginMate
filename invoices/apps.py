from django.apps import AppConfig


class InvoicesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'invoices'

    def ready(self):
        # Gather jobs run in a plain background thread (see tasks.py), which
        # cannot survive a server restart. Any job still marked PENDING/RUNNING
        # at startup was interrupted (dev server autoreload, crash, ...) and
        # would otherwise permanently block "Gather new invoices" from doing
        # anything, since trigger_gather refuses to start a second job while
        # one looks active.
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
