from __future__ import annotations

from django.utils import timezone

from .ai_suggestions import SuggestionCancelled, generate_suggestions_for_pending_products
from .models import Product, SuggestionJob


def run_suggestion_job(job_id: int) -> None:
    """Runs in a background thread (see views.py:trigger_suggest_products) so
    the request returns immediately and the review queue can poll for live
    progress instead of blocking for however long local inference takes."""
    job = SuggestionJob.objects.get(pk=job_id)
    job.status = SuggestionJob.Status.RUNNING
    job.total = Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=True).count()
    job.save(update_fields=["status", "total"])

    def on_batch_done(succeeded: int, failed: int) -> None:
        job.succeeded = succeeded
        job.failed = failed
        job.processed = succeeded + failed
        job.save(update_fields=["succeeded", "failed", "processed"])

    def should_cancel() -> bool:
        # Re-query rather than trust the in-memory `job` object, which this
        # background thread holds for the whole run - the cancel button sets
        # the flag from a completely different request/thread.
        return SuggestionJob.objects.filter(pk=job_id, cancel_requested=True).exists()

    try:
        generate_suggestions_for_pending_products(
            log=job.append_log, on_batch_done=on_batch_done, should_cancel=should_cancel
        )
        job.status = SuggestionJob.Status.SUCCESS
    except SuggestionCancelled:
        job.status = SuggestionJob.Status.CANCELLED
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI via the job log
        job.append_log(f"Échec : {exc}")
        job.status = SuggestionJob.Status.FAILED
    finally:
        job.finished_at = timezone.now()
        job.save()
