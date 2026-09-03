"""Background work for the sales import.

Runs on a plain thread (same as invoices/tasks.py) because driving a browser
through two date windows takes minutes, which is far too long to hold a
request open. The page polls SalesImportJob for progress.
"""

from __future__ import annotations

import traceback
from datetime import date

from django.conf import settings
from django.utils import timezone

from .models import PosProduct, SalesImportJob
from .pos.laddition_download import download_sales_lines
from .pos.laddition_xlsx import parse_sales_exports
from .sales import record_sales


class _Cancelled(Exception):
    pass


def _raise_if_cancelled(job: SalesImportJob) -> None:
    job.refresh_from_db(fields=["cancel_requested"])
    if job.cancel_requested:
        raise _Cancelled()


def sync_pos_products(export) -> int:
    """Record every till product the export mentioned, mapped or not.

    This is what turns "105 names printed once at the end of an import" into
    a backlog you can actually work through - see PosProduct. Totals
    accumulate across imports; re-importing the same period therefore
    double-counts them, which is why they're labelled as a rough guide to
    what sells rather than an accounting figure.
    """
    for name, info in export.products.items():
        product, created = PosProduct.objects.get_or_create(
            name=name,
            defaults={
                "category": info["category"],
                "typology": info["typology"],
                "total_quantity": info["quantity"],
                "first_seen": info["first"],
                "last_seen": info["last"],
            },
        )
        if created:
            continue
        product.total_quantity += info["quantity"]
        product.category = product.category or info["category"]
        product.typology = product.typology or info["typology"]
        product.first_seen = min(product.first_seen or info["first"], info["first"])
        product.last_seen = max(product.last_seen or info["last"], info["last"])
        product.save(update_fields=["total_quantity", "category", "typology", "first_seen", "last_seen"])
    return len(export.products)


def import_laddition_sales_task(job_id: int, start: date, end: date, download_dir: str | None = None) -> None:
    job = SalesImportJob.objects.get(pk=job_id)
    job.status = SalesImportJob.Status.RUNNING
    job.save(update_fields=["status"])
    download_dir = download_dir or str(settings.SCRAPE_DOWNLOAD_DIR)

    try:
        job.append_log(f"Récupération des ventes du {start} au {end}.")
        _raise_if_cancelled(job)

        paths = download_sales_lines(start, end, download_dir, log=job.append_log)
        if not paths:
            raise RuntimeError("Aucun fichier téléchargé.")
        _raise_if_cancelled(job)

        export = parse_sales_exports(paths)
        job.items_sold = export.total_quantity
        job.append_log(
            f"{len(export.entries)} totaux produit/jour lus ({export.total_quantity} articles vendus)."
        )

        seen = sync_pos_products(export)
        job.append_log(f"{seen} produits de caisse vus.")

        result = record_sales(export.entries, source="laddition")
        job.recorded = result.recorded
        job.unmatched = len(set(result.unmatched))
        job.append_log(
            f"{result.recorded} totaux recette/jour enregistrés "
            f"({result.created} nouveaux, {result.updated} mis à jour)."
        )
        if job.unmatched:
            job.append_log(
                f"{job.unmatched} produits de caisse sans recette - à traiter dans « Produits caisse »."
            )
        job.status = SalesImportJob.Status.SUCCESS

    except _Cancelled:
        job.status = SalesImportJob.Status.CANCELLED
        job.append_log("Annulé.")
    except Exception as exc:  # noqa: BLE001 - the job record IS the error report
        job.status = SalesImportJob.Status.FAILED
        job.append_log(f"Échec : {exc}")
        job.append_log(traceback.format_exc(limit=3))
    finally:
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "items_sold", "recorded", "unmatched"])
