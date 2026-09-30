"""Import sales from L'Addition for a date range.

    python manage.py laddition_import --from 2026-06-01 --to 2026-06-30
    python manage.py laddition_import --from 2023-12-01 --to 2026-09-02
    python manage.py laddition_import --file some-export.xlsx
    python manage.py laddition_import --from 2026-06-01 --to 2026-06-30 --dry-run

Downloads the "Lignes de ventes" export (splitting the range into windows
the back office will accept), reads it, and records what the import job
records, in its order: the till products and their days (« Ventes », with
the day's money), the recipes' sales, then the means of payment of every
till day it read (recipes/payments.py) - which --dry-run reads and reports
without writing. Ranges longer than two years are handled; --file skips the
download and reads one already downloaded.

In multi mode it runs for one espace (`manage.py tenant <dossier>
laddition_import …`) and downloads into that espace's own folder. Downloading
uses the server's L'Addition account, the owner's: refused elsewhere, like
the page's import (recipes/integration.py), and refused while the page's own
import runs - the two would sign in to one account at once and each take the
other's file from the folder. --file uses no account and is never refused.
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts import paths
from recipes.integration import refusal, require_espace, till_allowed
from recipes.models import SalesImportJob
from recipes.payments import record_payments
from recipes.pos.laddition_download import LadditionDownloadError, download_sales_lines
from recipes.pos.laddition_xlsx import LadditionExportError, parse_sales_exports
from recipes.pos.laddition_session import LadditionAuthError
from recipes.sales import record_sales
from recipes.tasks import payments_log, sync_pos_products


def _as_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"Not a YYYY-MM-DD date: {value!r}") from None


class Command(BaseCommand):
    help = "Import sales from L'Addition between two dates."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", help="First day, YYYY-MM-DD.")
        parser.add_argument("--to", dest="end", help="Last day, YYYY-MM-DD (inclusive).")
        parser.add_argument(
            "--file", action="append", default=[], dest="files",
            help="Read an already-downloaded export instead of fetching one. Repeatable.",
        )
        parser.add_argument(
            "--download-dir", default=None,
            help="Where to download (default: the espace's downloads folder).",
        )
        parser.add_argument("--no-headless", action="store_true", help="Show the browser.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Read and report, but write nothing - use it to check the names line up first.",
        )

    def handle(self, *args, **options):
        require_espace("laddition_import")
        files = options["files"]
        if not files:
            if not options["start"] or not options["end"]:
                raise CommandError("Give --from and --to, or --file.")
            start, end = _as_date(options["start"]), _as_date(options["end"])
            if start > end:
                raise CommandError("--from is after --to.")
            if not till_allowed():
                raise CommandError(refusal())
            # The page's own rule (views.trigger_sales_import), a dead run
            # reaped first so that it does not hold this up for ever.
            SalesImportJob.reap_stale()
            if SalesImportJob.objects.filter(
                status__in=[SalesImportJob.Status.PENDING, SalesImportJob.Status.RUNNING]
            ).exists():
                raise CommandError(
                    "Une récupération des ventes est déjà en cours dans l'application : attendez qu'elle finisse."
                )
            if options["no_headless"]:
                settings.SCRAPER_HEADLESS = False
            try:
                files = download_sales_lines(
                    start, end, options["download_dir"] or str(paths.downloads_dir()), log=self.stdout.write
                )
            except (LadditionAuthError, LadditionDownloadError) as exc:
                raise CommandError(str(exc)) from exc
            if not files:
                raise CommandError("Nothing was downloaded.")

        try:
            export = parse_sales_exports(files)
        except LadditionExportError as exc:
            raise CommandError(str(exc)) from exc

        covered = export.days
        self.stdout.write(
            f"Read {len(export.entries)} product/day totals "
            f"({export.total_quantity} items sold"
            + (f", {export.offered} of them offered" if export.offered else "")
            + f") covering {covered[0]} to {covered[1]}." if covered else "Read nothing."
        )
        if export.skipped:
            self.stdout.write(f"Ignored {export.skipped} row(s) with no usable date/name/quantity.")
        # The job's own French lines: one wording for what the payments
        # sheet said, wherever the import runs from.
        for message in payments_log(export):
            self.stdout.write(message)

        if options["dry_run"]:
            # Resolve names without writing, so the unmatched list can be
            # seen before anything is committed.
            from recipes.sales import recipe_lookup

            lookup = recipe_lookup()
            unknown = sorted({name for name, _d, _q in export.entries if name.lower() not in lookup})
            matched = len({name for name, _d, _q in export.entries}) - len(unknown)
            self.stdout.write(self.style.WARNING("Dry run - nothing written."))
            self.stdout.write(f"{matched} till product(s) match a recipe.")
            self._report_unmatched(unknown)
            return

        # The job's order exactly (tasks.import_laddition_sales_task): the
        # till products and their days - « Ventes », the day's money - then
        # the recipes' sales, then the payments. Without the first, this
        # command stored a day's card and cash with no takings behind them:
        # the very day the backfill refuses to write and « Remplacer »
        # prunes. One rule for every writer.
        seen = sync_pos_products(export)
        self.stdout.write(f"{seen} till product(s) seen.")

        result = record_sales(export.entries, source="laddition")
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded {result.recorded} recipe/day totals "
                f"({result.created} new, {result.updated} updated)."
            )
        )
        self._report_unmatched(sorted(set(result.unmatched)))

        if export.payments_read:
            paid = record_payments(export)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Recorded the payments of {paid.days_written} till day(s) "
                    f"({paid.days_unchanged} already up to date)."
                )
            )

    def _report_unmatched(self, names):
        if not names:
            self.stdout.write(self.style.SUCCESS("Every till product matched a recipe."))
            return
        self.stdout.write(
            self.style.WARNING(
                f"\n{len(names)} till product(s) match no recipe, so their sales were NOT "
                "recorded. Until they are, whatever stock they consume will show up as "
                "missing in the variance report:"
            )
        )
        for name in names:
            self.stdout.write(f"  - {name}")
        self.stdout.write(
            "\nCreate a recipe with exactly that name, or - for a happy-hour variant - "
            "put the name in the base recipe's \"Nom en happy hour sur la caisse\" field."
        )
