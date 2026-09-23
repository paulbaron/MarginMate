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


def _euros(value) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def money_log(export) -> list[str]:
    """What the money columns said, in the import's own log.

    Everything unusual is named: a rate that could not be read (so that
    revenue has no HT), a discount (a column that has been 0,00 on every
    line ever exported), a quantity that is not 1 (never seen - and the
    line's amount is still its own, whatever it says). Left silent, each of
    those is a margin quietly too large.
    """
    if not export.money_columns:
        return ["Ce fichier ne porte pas les colonnes de prix : aucune recette lue."]
    lines = [
        f"Recettes lues : {_euros(export.revenue_ttc)} TTC, {_euros(export.revenue_ht)} HT."
    ]
    if export.lines_without_rate:
        lines.append(
            f"{export.lines_without_rate} ligne(s) sans taux lisible : "
            f"{_euros(export.revenue_without_rate_ttc)} TTC sans HT (aucun taux supposé)."
        )
    if export.lines_without_amount:
        lines.append(f"{export.lines_without_amount} ligne(s) sans montant lisible.")
    if export.days_without_amount:
        lines.append(
            f"{export.days_without_amount} (produit, jour) laissé(s) non lu(s) : une de leurs "
            "lignes n'a pas de montant, donc leur recette serait trop basse d'un montant inconnu."
        )
    if export.discounted_lines:
        lines.append(
            f"{export.discounted_lines} ligne(s) avec remise, {_euros(export.discount_ttc)} déduits."
        )
    if export.discounts_not_taken:
        lines.append(
            f"{export.discounts_not_taken} ligne(s) dont la remise n'a pas été déduite : "
            "elle rendrait la ligne plus grosse que son propre prix - à vérifier."
        )
    if export.refund_lines:
        lines.append(f"{export.refund_lines} ligne(s) de remboursement (quantité négative).")
    if export.unusual_quantity_lines:
        lines.append(
            f"{export.unusual_quantity_lines} ligne(s) à une quantité autre que 1 : le montant lu "
            "reste « Prix TTC », celui de la ligne - à vérifier."
        )
    if export.repeated_days:
        lines.append(
            f"{export.repeated_days} (produit, jour) lus dans deux fichiers : la dernière lecture "
            "remplace, rien ne s'additionne."
        )
    return lines


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

    The day's money (`export.money`) is written the same way, and only for
    the days the export actually priced - see below.
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

    # The money goes in exactly as idempotently, and only where it was
    # actually read. A day the export carried no money for keeps the money
    # it already has: re-importing an older download (no `Prix TTC` column
    # at all), or one window of a period already backfilled, must not quietly
    # zero a day - a lost revenue reads as a 100 % margin, and nothing on
    # any page would contradict it.
    # getattr, because the money is optional on this shape: an export with
    # no price columns has none, and a caller building the minimum this
    # function needs (products + entries) must keep working.
    money = getattr(export, "money", None) or {}
    priced = [key for key in daily if key in money]
    unpriced = [key for key in daily if key not in money]

    if priced:
        PosProductDailyQuantity.objects.bulk_create(
            [
                PosProductDailyQuantity(
                    product=products[name],
                    sold_on=day,
                    quantity=daily[(name, day)],
                    revenue_ttc=money[(name, day)].revenue_ttc,
                    revenue_ht=money[(name, day)].revenue_ht,
                    revenue_without_rate_ttc=money[(name, day)].without_rate_ttc,
                    revenue_read=True,
                )
                for name, day in priced
            ],
            update_conflicts=True,
            unique_fields=["product", "sold_on"],
            update_fields=["quantity", "revenue_ttc", "revenue_ht", "revenue_without_rate_ttc", "revenue_read"],
        )
    if unpriced:
        PosProductDailyQuantity.objects.bulk_create(
            [
                PosProductDailyQuantity(product=products[name], sold_on=day, quantity=daily[(name, day)])
                for name, day in unpriced
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
        for message in money_log(export):
            job.append_log(message)

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
