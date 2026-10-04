"""Background work for the sales import.

Runs on a plain thread (same as invoices/tasks.py) because driving a browser
through two date windows takes minutes, which is far too long to hold a
request open. The page polls SalesImportJob for progress.

`store_reading` is the ONE writer of a till reading, whoever read it - the
job fetching L'Addition, `laddition_import`, a file uploaded on « Ventes »:
the till products and their days (« Ventes », with the day's money), the
recipes' sales under `sales.TILL_SOURCE`, then the payments - in that
order, each said in the log.

The job's log is drawn on the sales page of whichever espace ran it, so a
failure says what `common.error_for_page` lets a page say (LB-3): the
till's own French refusals as they are, anything else one fixed sentence.
The exception and its traceback always go to the server's log; the
traceback is added to the job's log in the server-accounts espace only
(`accounts.tenancy.server_accounts_allowed`, the owner's - as before).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.text import get_valid_filename

from accounts.paths import downloads_dir, imports_dir
from accounts.tenancy import server_accounts_allowed
from common import error_for_page, format_money

from .integration import refusal, till_allowed
from .models import PosDailyPayment, PosProduct, PosProductDailyQuantity, SalesImportJob
from .payments import RecordedPayments, by_method, oddities, record_payments
from .pos.connectors import read_upload, resolve
from .pos.laddition_download import DownloadCancelled, LadditionDownloadError, download_sales_lines
from .pos.laddition_session import LadditionAuthError
from .pos.laddition_xlsx import LadditionExportError, parse_sales_exports
from .pos.till_file import TillFileError
from .pos.xlsx_reader import XlsxError
from .sales import TILL_SOURCE, SalesImportResult, recipe_lookup, record_sales
from .sales_sources import LADDITION

logger = logging.getLogger(__name__)


class TillImportError(RuntimeError):
    """A till import that cannot go on, said in French for the job's log."""


#: The till's own refusals: French, naming no path - a job's log says them
#: as they are (`common.error_for_page`). Anything else is one fixed sentence.
TILL_REFUSALS: tuple[type[BaseException], ...] = (
    TillImportError,
    LadditionExportError,
    LadditionAuthError,
    LadditionDownloadError,
    XlsxError,
    TillFileError,
)


class _Cancelled(Exception):
    pass


def _euros(value) -> str:
    """1234.5 -> « 1 234,50 € », grouped by a no-break space: the job's log
    is read on the sales page."""
    return format_money(value).replace(".", ",") + " €"


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
    lines = [f"Recettes lues : {_euros(export.revenue_ttc)} TTC, {_euros(export.revenue_ht)} HT."]
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
        lines.append(f"{export.discounted_lines} ligne(s) avec remise, {_euros(export.discount_ttc)} déduits.")
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


def payments_log(export) -> list[str]:
    """What the payments sheet said, in the import's own log - the same
    care as money_log: a sheet that did not read, a ticket that did not,
    a ticket paid nothing for a total, all named. The lines import either
    way; only the payments are at stake here."""
    lines = [f"Feuille des tickets illisible, paiements non lus : {problem}" for problem in export.payment_sheet_errors]
    if not export.payments_read:
        if not lines:
            lines.append(
                "Ce fichier ne porte pas la feuille des tickets (SalesDocument) : aucun moyen de "
                "paiement lu, ceux déjà enregistrés restent."
            )
        return lines
    methods = ", ".join(
        f"{PosDailyPayment.label_for(method)} {_euros(payment.amount)}"
        for method, payment in by_method(export.payments_by_method())
    )
    lines.append(
        f"Paiements lus : {_euros(export.payments_total)} sur {len(export.payment_days)} jour(s), "
        f"{export.tickets} ticket(s)" + (f" ({methods})." if methods else ".")
    )
    lines.extend(oddities(export))
    if export.payment_sheets_missing:
        lines.append(
            f"{export.payment_sheets_missing} fichier(s) sans feuille des tickets : leurs jours gardent "
            "les paiements déjà enregistrés."
        )
    if export.repeated_payment_days:
        lines.append(
            f"{export.repeated_payment_days} jour(s) de paiements lus dans deux fichiers : la dernière "
            "lecture remplace, rien ne s'additionne."
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


def till_entries(export) -> list[tuple[str, date, int]]:
    """What `record_sales` is handed for a reading: the reading's own
    (product, day) quantities, then every OTHER till product's quantity
    already on file (`PosProductDailyQuantity`) for the days it touched.

    `record_sales` sets each (recipe, day) to the sum it is handed, while
    `sync_pos_products` replaces only the products a reading holds. Handed
    the reading alone, a file holding one product of a day set the recipe to
    that product's quantity while the day kept its others: a pint corrected
    to 4 beside its happy-hour name's 3 left the recipe at 4 - « Vendu » and
    « Écarts » read 4, Marges read 7, and the next link changed went back to
    7. So the recipe's day is what the day's till products say, which is
    what `resync_recipe_from_daily_quantities` rebuilds too. A fetch reads
    whole days: beside its own entries it finds only a product an earlier
    reading of the day held and this one no longer prints."""
    entries = list(export.entries)
    days = {day for _name, day, _quantity in entries}
    if not days:
        return entries
    read = {(name, day) for name, day, _quantity in entries}
    beside = (
        PosProductDailyQuantity.objects.filter(sold_on__range=(min(days), max(days)))
        .order_by("sold_on", "product__name")
        .values_list("product__name", "sold_on", "quantity")
    )
    entries.extend(
        (name, day, quantity) for name, day, quantity in beside.iterator() if day in days and (name, day) not in read
    )
    return entries


@dataclass
class StoredReading:
    """What `store_reading` wrote."""

    #: Till products the reading named (sync_pos_products).
    seen: int = 0
    sales: SalesImportResult = field(default_factory=SalesImportResult)
    paid: RecordedPayments = field(default_factory=RecordedPayments)

    @property
    def unmatched(self) -> int:
        return len(set(self.sales.unmatched))


def store_reading(export, log, *, payments_beside_sales: bool = False) -> StoredReading:
    """Write one till reading, in the one order every writer keeps: the till
    products and their days (« Ventes », with the day's money), the
    recipes' sales (`TILL_SOURCE`, never a sale typed by hand), then the
    payments - each step said through `log` in French.

    A reading of payments alone (`export.sales_read` False: a file of
    « Encaissements ») writes no sales and says nothing of them.
    `payments_beside_sales`: its days are written only where « Ventes » holds
    a sale (recipes.payments.record_payments), the others said.

    The recipes' sales of the days read are worked out from every till
    product of those days, the reading's and those already on file
    (`till_entries`): a file holding part of a day leaves the recipes
    agreeing with the day. The products said to have no recipe are the
    reading's own."""
    stored = StoredReading()
    if getattr(export, "sales_read", True):
        stored.seen = sync_pos_products(export)
        log(f"{stored.seen} produits de caisse vus.")
        stored.sales = record_sales(till_entries(export), source=TILL_SOURCE)
        named = {str(name).strip() for name, _day, _quantity in export.entries}
        stored.sales.unmatched = [name for name in stored.sales.unmatched if name in named]
        log(
            f"{stored.sales.recorded} totaux recette/jour enregistrés "
            f"({stored.sales.created} nouveaux, {stored.sales.updated} mis à jour)."
        )
        if stored.unmatched:
            log(f"{stored.unmatched} produits de caisse sans recette - à traiter dans « À lier ».")
    # After the sales, and on its own: what the bank is paid from, per day
    # and per means of payment (recipes/payments.py).
    stored.paid = record_payments(export, beside_sales=payments_beside_sales)
    if export.payments_read:
        log(
            f"Paiements enregistrés : {stored.paid.days_written} jour(s) de caisse remplacé(s), "
            f"{stored.paid.days_unchanged} déjà à jour."
        )
        if stored.paid.days_without_sales:
            log(
                f"{len(stored.paid.days_without_sales)} jour(s) laissé(s) de côté : aucune vente enregistrée ces "
                "jours-là. Importez d'abord les ventes de ces jours."
            )
    return stored


def fail(job: SalesImportJob, exc: BaseException, what: str) -> str:
    """The job's failure line: the till's own French refusal as it is, else
    one fixed sentence - the exception always in the server's log, its
    traceback in the job's log in the server-accounts espace only.

    Returns what an automatic import's alert says of it (auto_sales.finish):
    the exception's own words in the server-accounts espace, as they always
    were, the job's sentence anywhere else - an alert names no library's
    English, no server path."""
    logger.warning("%s : échec", what, exc_info=(type(exc), exc, exc.__traceback__))
    job.status = SalesImportJob.Status.FAILED
    said = error_for_page(exc, said=TILL_REFUSALS)
    job.append_log("Échec : " + said)
    if server_accounts_allowed():
        job.append_log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=3)))
        return str(exc).strip() or exc.__class__.__name__
    return said


def import_laddition_sales_task(job_id: int, start: date, end: date, download_dir: str | None = None) -> None:
    """The thread's body, started as ``target=bound(import_laddition_sales_task)``
    (importing.start_sales_import, from the Ventes tab or an automatic
    import): it runs bound to the tenant that asked, so the job, the sales
    and the download folder are that tenant's, and its connections are
    closed when it ends.

    Its final status is saved by `auto_sales.finish`, which says how it
    went: a successful import moves the till's coverage (where the next
    automatic import starts) in the same transaction as its status, an
    automatic one that succeeded or failed sends its alert - never a
    cancelled one, nor one refused above."""
    from . import auto_sales

    job = SalesImportJob.objects.get(pk=job_id)
    if not till_allowed():
        # The page refuses first; this is the thread's own guard, before
        # anything is downloaded. A sentence, not a traceback.
        job.status = SalesImportJob.Status.FAILED
        job.finished_at = timezone.now()
        job.append_log(refusal())
        job.save(update_fields=["status", "finished_at"])
        return
    job.status = SalesImportJob.Status.RUNNING
    job.save(update_fields=["status"])
    # The tenant's own folder: the download takes the first new .xlsx that
    # lands in it.
    download_dir = download_dir or str(downloads_dir())
    own = None
    error = ""

    try:
        # The till's own start, worked out BEFORE the import, whose sales move
        # it (auto_sales.own_start; the gathers' rule).
        own = auto_sales.own_start(timezone.localdate())
        job.append_log(f"Récupération des ventes du {start} au {end}.")
        _raise_if_cancelled(job)

        def still_wanted() -> bool:
            # Also a heartbeat: the download is the long phase, and a run
            # silent for ten minutes is treated as dead (see is_stale).
            job.beat()
            job.refresh_from_db(fields=["cancel_requested"])
            return job.cancel_requested

        paths = download_sales_lines(start, end, download_dir, log=job.append_log, should_cancel=still_wanted)
        if not paths:
            raise TillImportError("Aucun fichier téléchargé.")
        _raise_if_cancelled(job)

        export = parse_sales_exports(paths)
        job.items_sold = export.total_quantity
        job.append_log(f"{len(export.entries)} totaux produit/jour lus ({export.total_quantity} unités vendues).")
        for message in money_log(export):
            job.append_log(message)
        for message in payments_log(export):
            job.append_log(message)

        stored = store_reading(export, job.append_log)
        job.recorded = stored.sales.recorded
        job.unmatched = stored.unmatched
        job.status = SalesImportJob.Status.SUCCESS

    except (_Cancelled, DownloadCancelled):
        job.status = SalesImportJob.Status.CANCELLED
        job.append_log("Annulé.")
    except Exception as exc:  # noqa: BLE001 - the job record IS the error report
        error = fail(job, exc, "Import des ventes de L'Addition")
    finally:
        job.finished_at = timezone.now()
        # The status and, for a SUCCESS, the coverage it records commit
        # together: a tick between the two saw the import over and its days
        # not covered, and signed in again for them.
        auto_sales.finish(
            job,
            fields=["status", "finished_at", "items_sold", "recorded", "unmatched"],
            source_key=LADDITION,
            own=own,
            error=error,
        )


# -- a file of the till, uploaded on « Ventes » --------------------------------------------

#: Where a till's files are kept once read: L'Addition's own export goes to
#: the espace's downloads/ (the backfills glob downloads/*.xlsx), any other
#: till's file to downloads/caisse/ - the newest KEPT_FILES uploads of each,
#: each content once.
TILL_FILES = "caisse"
KEPT_FILES = 50
#: What a kept upload's name starts with: in downloads/ it tells the uploads
#: from the fetch's own downloads, which are never pruned.
UPLOADED = "televerse"
#: How much of the file's SHA-256 its kept name carries: the same file
#: uploaded again is kept once (downloads/ is in every backup).
DIGEST_CHARS = 16
#: How long a kept file's name may be: Windows' paths are short.
KEPT_NAME_LENGTH = 80


def till_files_dir() -> Path:
    folder = downloads_dir() / TILL_FILES
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def staged_uploads_dir() -> Path:
    """Where an upload waits for its reading: the espace's imports/, never
    downloads/ - a half-read file there would be read by the backfills, or
    taken for L'Addition's download by a fetch."""
    folder = imports_dir() / TILL_FILES
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()[:DIGEST_CHARS]


def _kept_name(folder: Path, file_name: str, digest: str) -> Path:
    """« televerse-20261004-153012-<empreinte>-export_caisse.csv », free in
    `folder`: the mark, the moment, the content's digest, then the file's own
    name made safe and cut, its suffix kept."""
    original = Path(str(file_name or "")).name
    suffix = Path(original).suffix.lower()[:5]
    try:
        safe = get_valid_filename(Path(original).stem)
    except SuspiciousFileOperation:
        safe = "fichier"
    stem = f"{UPLOADED}-{timezone.localtime():%Y%m%d-%H%M%S}-{digest}-{safe[:KEPT_NAME_LENGTH]}"
    target = folder / f"{stem}{suffix}"
    number = 2
    while target.exists():
        target = folder / f"{stem}-{number}{suffix}"
        number += 1
    return target


def keep_upload(staged: Path, file_name: str, laddition: bool) -> Path:
    """Move a file read whole into its place - `downloads/` for
    L'Addition's export, `downloads/caisse/` for any other - and keep the
    newest KEPT_FILES uploads there. The same content already kept (its
    digest is in the name) is kept once: the staged copy goes. In
    downloads/ only the uploads are pruned, never the fetch's downloads."""
    folder = downloads_dir() if laddition else till_files_dir()
    digest = _digest(staged)
    same = sorted(path for path in folder.glob(f"{UPLOADED}-*-{digest}-*") if path.is_file())
    if same:
        staged.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            os.utime(same[-1])
        return same[-1]
    target = _kept_name(folder, file_name, digest)
    os.replace(staged, target)
    uploads = folder.glob(f"{UPLOADED}-*") if laddition else folder.iterdir()
    kept = sorted((path for path in uploads if path.is_file()), key=lambda path: path.stat().st_mtime)
    for old in kept[:-KEPT_FILES]:
        with contextlib.suppress(OSError):
            old.unlink()
    return target


def reading_log(export) -> list[str]:
    """What a till's file read, for the job's log - the sales' or the
    payments', in French."""
    lines = []
    if export.sales_read:
        lines.append(f"{len(export.entries)} totaux produit/jour lus ({export.total_quantity} unités vendues).")
        if export.skipped:
            lines.append(f"{export.skipped} ligne(s) sans jour ou sans produit passée(s).")
        if export.quantities_rounded:
            lines.append(
                f"{export.quantities_rounded} (produit, jour) à une quantité fractionnaire, arrondie à l'unité la "
                "plus proche."
            )
        lines.extend(money_log(export))
        return lines
    methods = ", ".join(
        f"{PosDailyPayment.label_for(method)} {_euros(payment.amount)}"
        for method, payment in by_method(export.payments_by_method())
    )
    lines.append(
        f"Paiements lus : {_euros(export.payments_total)} sur {len(export.payment_days)} jour(s), "
        f"{export.tickets} paiement(s)" + (f" ({methods})." if methods else ".")
    )
    if export.skipped:
        lines.append(f"{export.skipped} ligne(s) sans jour ou sans paiement passée(s).")
    if export.unread_payment_tickets:
        lines.append(
            f"{export.unread_payment_tickets} paiement(s) sans moyen : classé(s) "
            f"« {PosDailyPayment.LABELS[PosDailyPayment.UNREAD]} »."
        )
    if export.unmapped_methods:
        printed = ", ".join(f"« {method} »" for method in export.unmapped_methods)
        lines.append(
            f"Moyens de paiement gardés tels qu'imprimés : {printed} - à faire correspondre dans le format pour "
            "qu'ils comptent comme Carte, Espèces…"
        )
    return lines


def _covered(export) -> tuple[date | None, date | None]:
    days = [day for _name, day, _quantity in export.entries] or list(export.payment_days)
    return (min(days), max(days)) if days else (None, None)


def import_till_file_task(
    job_id: int, staged_path: str, choice_value: str, file_name: str, day: date | None = None, uploaded_by: str = ""
) -> None:
    """The thread's body, started as ``target=bound(import_till_file_task)``
    (till_views.upload_sales_file): a file uploaded on « Ventes », waiting
    under `staged_path`, read with its choice - L'Addition's export or a
    format - then written by `store_reading`, the payments beside the days
    « Ventes » holds. Read whole, the file is moved into its place
    (`keep_upload`); refused, it is deleted. The choice is resolved again
    here: the format may have been edited or deleted since the upload."""
    job = SalesImportJob.objects.get(pk=job_id)
    job.status = SalesImportJob.Status.RUNNING
    job.save(update_fields=["status"])
    staged = Path(staged_path)

    def alive() -> None:
        # The reading is silent, and a run silent for ten minutes is reaped
        # (common's STALE_AFTER): a second upload or fetch could then start
        # beside it. A heartbeat every few thousand rows, and a cancel heard.
        job.beat()
        _raise_if_cancelled(job)

    try:
        job.append_log(f"Fichier « {Path(file_name).name} »." + (f" Importé par {uploaded_by}." if uploaded_by else ""))
        _raise_if_cancelled(job)
        choice = resolve(choice_value, file_name)
        job.append_log(f"Format « {choice.label} »." + (f" Jour des ventes : {day:%d/%m/%Y}." if day else ""))
        export = read_upload(staged, choice, file_name=file_name, day=day, progress=alive)
        _raise_if_cancelled(job)
        keep_upload(staged, file_name, choice.laddition)
        job.range_start, job.range_end = _covered(export)
        job.items_sold = export.total_quantity
        for message in reading_log(export):
            job.append_log(message)
        if choice.laddition:
            for message in payments_log(export):
                job.append_log(message)
        stored = store_reading(export, job.append_log, payments_beside_sales=True)
        job.recorded = stored.sales.recorded
        job.unmatched = stored.unmatched
        job.status = SalesImportJob.Status.SUCCESS
    except _Cancelled:
        job.status = SalesImportJob.Status.CANCELLED
        job.append_log("Annulé.")
    except Exception as exc:  # noqa: BLE001 - the job record IS the error report
        fail(job, exc, "Import d'un fichier de caisse")
    finally:
        # A file not moved into its place was not read whole: it goes.
        with contextlib.suppress(OSError):
            staged.unlink(missing_ok=True)
        job.finished_at = timezone.now()
        job.save(
            update_fields=["status", "finished_at", "items_sold", "recorded", "unmatched", "range_start", "range_end"]
        )
