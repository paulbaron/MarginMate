from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        # SuggestionJob runs in a plain background thread (see tasks.py),
        # which cannot survive a server restart. Any job still marked
        # PENDING/RUNNING at startup was interrupted and would otherwise
        # permanently block "Suggérer avec l'IA" from doing anything, since
        # trigger_suggest_products refuses to start a second job while one
        # looks active (same issue already hit once for invoices.ScrapeJob).
        import warnings

        from django.db.utils import OperationalError
        from django.utils import timezone

        from .models import SuggestionJob

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                stuck_jobs = list(
                    SuggestionJob.objects.filter(
                        status__in=[SuggestionJob.Status.PENDING, SuggestionJob.Status.RUNNING]
                    )
                )
                for job in stuck_jobs:
                    job.append_log("Interrompu par un redémarrage du serveur.")
                    job.status = SuggestionJob.Status.FAILED
                    job.finished_at = timezone.now()
                    job.save(update_fields=["status", "finished_at"])
        except OperationalError:
            pass  # database/migrations not ready yet (e.g. during `migrate` itself)
