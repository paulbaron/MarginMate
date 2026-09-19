"""Background work for the sales import.

Runs on a plain thread (same as invoices/tasks.py) because driving a browser
through two date windows takes minutes, which is far too long to hold a
request open. The page polls SalesImportJob for progress.
"""

from __future__ import annotations

import traceback

from django.db import transaction
from django.db.models import Sum
from datetime import date

from django.conf import settings
from django.utils import timezone

from .models import PosProduct, PosProductDailyQuantity, SalesImportJob
from .pos.laddition_download import DownloadCancelled, download_sales_lines
from .pos.laddition_xlsx import parse_sales_exports
from .sales import record_sales, recipe_lookup


class _Cancelled(Exception):
    pass


def _raise_if_cancelled(job: SalesImportJob) -> None:
    job.refresh_from_db(fields=["cancel_requested"])
    if job.cancel_requested:
        raise _Cancelled()


def sync_pos_products(export) -> int:
    """Record every till product the export mentioned, mapped or not.

    This is what turns "105 names printed once at the end of an import" into
    a backlog you can actually work through - see PosProduct. Quantities are
    tracked per day (PosProductDailyQuantity), which is what makes this safe
    to call on an already-covered period: a re-imported day corrects itself
    instead of piling onto what was already recorded for it - the same
    guarantee record_sales already gives RecipeSale. See that model's
    docstring for what the naive version used to do instead.
    """
    # One transaction: same lock contention as record_sales.
    with transaction.atomic():
        return _sync_pos_products(export)


def _sync_pos_products(export) -> int:
    # A till name that already answers to a recipe - its own name, or a
    # happy_hour_name set on one - is counted into that recipe's sales by
    # record_sales whether or not this PosProduct is explicitly linked (see
    # recipe_lookup). Left unlinked, it sits in the "needs review" backlog
    # forever asking to be resolved even though it already has been, and
    # "Produits caisse" undercounts that recipe relative to what "Dernières
    # ventes" shows for it - the two pages stop reconciling. Auto-linking
    # here doesn't change what gets counted, only makes the explicit link
    # match what record_sales was already doing silently.
    lookup = recipe_lookup()

    products: dict[str, PosProduct] = {}
    for name, info in export.products.items():
        product, created = PosProduct.objects.get_or_create(
            name=name,
            defaults={
                "category": info["category"],
                "typology": info["typology"],
                "first_seen": info["first"],
                "last_seen": info["last"],
            },
        )
        update_fields = []
        if not created:
            product.category = product.category or info["category"]
            product.typology = product.typology or info["typology"]
            product.first_seen = min(product.first_seen or info["first"], info["first"])
            product.last_seen = max(product.last_seen or info["last"], info["last"])
            update_fields = ["category", "typology", "first_seen", "last_seen"]
        if product.recipe_id is None and not product.ignored:
            coinciding = lookup.get(name.strip().lower())
            if coinciding is not None:
                product.recipe = coinciding
                update_fields.append("recipe")
        if update_fields:
            product.save(update_fields=update_fields)
        products[name] = product

    # (name, day) -> quantity, from the same per-day entries record_sales
    # works from, rather than export.products' window-wide total - that's
    # the whole fix: a day sold twice in one export sums (one row per item
    # rung up), but a day already recorded from an EARLIER import is
    # replaced, not added to.
    daily: dict[tuple[str, date], int] = {}
    for name, day, quantity in export.entries:
        key = (name, day)
        daily[key] = daily.get(key, 0) + quantity

    PosProductDailyQuantity.objects.bulk_create(
        [
            PosProductDailyQuantity(product=products[name], sold_on=day, quantity=quantity)
            for (name, day), quantity in daily.items()
        ],
        update_conflicts=True,
        unique_fields=["product", "sold_on"],
        update_fields=["quantity"],
    )

    touched = list(products.values())
    totals = {
        row["product_id"]: row["total"]
        for row in PosProductDailyQuantity.objects.filter(product__in=touched)
        .values("product_id")
        .annotate(total=Sum("quantity"))
    }
    changed = []
    for product in touched:
        total = totals.get(product.id, 0)
        if total != product.total_quantity:
            product.total_quantity = total
            changed.append(product)
    if changed:
        PosProduct.objects.bulk_update(changed, ["total_quantity"])

    return len(export.products)


def import_laddition_sales_task(job_id: int, start: date, end: date, download_dir: str | None = None) -> None:
    job = SalesImportJob.objects.get(pk=job_id)
    job.status = SalesImportJob.Status.RUNNING
    job.save(update_fields=["status"])
    download_dir = download_dir or str(settings.SCRAPE_DOWNLOAD_DIR)

    try:
        job.append_log(f"Récupération des ventes du {start} au {end}.")
        _raise_if_cancelled(job)

        def still_wanted() -> bool:
            # Also a heartbeat: the download is the long phase, and a run
            # silent for ten minutes is treated as dead (see is_stale).
            job.beat()
            job.refresh_from_db(fields=["cancel_requested"])
            return job.cancel_requested

        paths = download_sales_lines(
            start, end, download_dir, log=job.append_log, should_cancel=still_wanted
        )
        if not paths:
            raise RuntimeError("Aucun fichier téléchargé.")
        _raise_if_cancelled(job)

        export = parse_sales_exports(paths)
        job.items_sold = export.total_quantity
        job.append_log(
            f"{len(export.entries)} totaux produit/jour lus ({export.total_quantity} unités vendues)."
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

    except (_Cancelled, DownloadCancelled):
        job.status = SalesImportJob.Status.CANCELLED
        job.append_log("Annulé.")
    except Exception as exc:  # noqa: BLE001 - the job record IS the error report
        job.status = SalesImportJob.Status.FAILED
        job.append_log(f"Échec : {exc}")
        job.append_log(traceback.format_exc(limit=3))
    finally:
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "items_sold", "recorded", "unmatched"])
