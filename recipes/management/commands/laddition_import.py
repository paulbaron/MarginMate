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

It runs for one tenant (`manage.py tenant <folder> laddition_import …`)
and downloads into that tenant's own folder, signing in with that espace's
own L'Addition account - its « Identifiants », the .env standing in for the
platform owner's espace alone -: refused unbound, like the page's import
(recipes/integration.py). The job's failure line is the till's own French
refusal or one fixed sentence (tasks.fail): the Ventes tab of the espace
shows it.

**A download holds the one sales import's lock** (importing.claim_sales_import,
the Ventes tab's and the scheduler's): a manual SalesImportJob of the period,
RUNNING while the command runs and beating as the download goes (a run
silent for ten minutes is reaped as dead), ended SUCCESS, FAILED or
CANCELLED - « Annuler » on the Ventes tab stops it - with the coverage a
SUCCESS records (auto_sales.finish). So it is refused while another import
runs, and neither the tab nor an automatic import starts beside it: two
sign-ins to one account at once, each taking the other's file from the
shared folder, an automatic import then recording coverage for days it
never imported. A dry run's job is deleted once it ends well: it recorded
nothing. --file uses no account, makes no job and is never refused.
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts import paths
from recipes import auto_sales
from recipes.importing import claim_sales_import
from recipes.integration import refusal, require_tenant_for_command, till_allowed
from recipes.models import SalesImportJob
from recipes.pos.laddition_download import DownloadCancelled, LadditionDownloadError, download_sales_lines
from recipes.pos.laddition_session import LadditionAuthError
from recipes.pos.laddition_xlsx import LadditionExportError, parse_sales_exports
from recipes.sales_sources import LADDITION
from recipes.tasks import fail, payments_log, store_reading

BUSY = "Une récupération des ventes est déjà en cours dans l'application : attendez qu'elle finisse."
#: The first line of the command's job, as the Ventes tab shows it.
COMMAND_NOTE = "Lancé par la commande laddition_import."
CANCELLED = "Annulé depuis l'application."
#: What the server's log calls a failure of the command's job.
COMMAND_FAILED = "Commande laddition_import"
#: What the command's job saves when it ends.
END_FIELDS = ["status", "finished_at", "items_sold", "recorded", "unmatched"]


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
            "--file",
            action="append",
            default=[],
            dest="files",
            help="Read an already-downloaded export instead of fetching one. Repeatable.",
        )
        parser.add_argument(
            "--download-dir",
            default=None,
            help="Where to download (default: the espace's downloads folder).",
        )
        parser.add_argument("--no-headless", action="store_true", help="Show the browser.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and report, but write nothing - use it to check the names line up first.",
        )

    def handle(self, *args, **options):
        require_tenant_for_command("laddition_import")
        files = options["files"]
        if files:
            self._read_and_record(files, options)
            return
        if not options["start"] or not options["end"]:
            raise CommandError("Give --from and --to, or --file.")
        start, end = _as_date(options["start"]), _as_date(options["end"])
        if start > end:
            raise CommandError("--from is after --to.")
        if not till_allowed():
            raise CommandError(refusal())
        # The lock the page and the scheduler take (a dead run reaped first,
        # so that it does not hold this up for ever).
        job = claim_sales_import(start, end, trigger=SalesImportJob.Trigger.MANUAL, notes=[COMMAND_NOTE])
        if not isinstance(job, SalesImportJob):  # None: another import runs (no plan, so never a sentence)
            raise CommandError(BUSY)
        if options["no_headless"]:
            settings.SCRAPER_HEADLESS = False
        # The till's own start, worked out BEFORE the import, whose sales move
        # it (the task's rule).
        own = auto_sales.own_start(timezone.localdate())
        job.status = SalesImportJob.Status.RUNNING
        job.save(update_fields=["status"])
        status = SalesImportJob.Status.FAILED
        try:
            files = self._download(job, start, end, options)
            self._read_and_record(files, options, job=job)
            status = SalesImportJob.Status.SUCCESS
        except DownloadCancelled:
            status = SalesImportJob.Status.CANCELLED
            job.append_log("Annulé.")
            raise CommandError(CANCELLED) from None
        except Exception as exc:  # said in the job's log, then raised as it was
            # A refused sign-in arrives wrapped in a CommandError: its own
            # French words are what the Ventes tab says.
            fail(job, exc.__cause__ if isinstance(exc, CommandError) and exc.__cause__ else exc, COMMAND_FAILED)
            raise
        finally:
            self._end(job, status, own, dry_run=options["dry_run"])

    def _download(self, job, start, end, options) -> list[str]:
        def log(message: str) -> None:
            self.stdout.write(message)
            job.append_log(message)

        def still_wanted() -> bool:
            # Also a heartbeat: a run silent for ten minutes is reaped as
            # dead, and the lock would go with it.
            job.beat()
            job.refresh_from_db(fields=["cancel_requested"])
            return job.cancel_requested

        try:
            files = download_sales_lines(
                start,
                end,
                options["download_dir"] or str(paths.downloads_dir()),
                log=log,
                should_cancel=still_wanted,
            )
        except (LadditionAuthError, LadditionDownloadError) as exc:
            raise CommandError(str(exc)) from exc
        if not files:
            raise CommandError("Nothing was downloaded.")
        return files

    def _end(self, job, status, own, *, dry_run: bool) -> None:
        """The command's job ends: deleted after a dry run that went well
        (it recorded nothing), else its status saved - with, for a SUCCESS,
        the coverage it records."""
        if dry_run and status == SalesImportJob.Status.SUCCESS:
            job.delete()
            return
        job.status = status
        job.finished_at = timezone.now()
        auto_sales.finish(job, fields=END_FIELDS, source_key=LADDITION, own=own)

    def _read_and_record(self, files, options, job=None) -> None:
        if job is not None:
            job.beat()
        try:
            export = parse_sales_exports(files)
        except LadditionExportError as exc:
            raise CommandError(str(exc)) from exc

        covered = export.days
        self.stdout.write(
            f"Read {len(export.entries)} product/day totals "
            f"({export.total_quantity} items sold"
            + (f", {export.offered} of them offered" if export.offered else "")
            + f") covering {covered[0]} to {covered[1]}."
            if covered
            else "Read nothing."
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

        # The job's writer exactly (tasks.store_reading): the till products
        # and their days - « Ventes », the day's money - then the recipes'
        # sales, then the payments, each said in the job's own words.
        # Without the first, this command once stored a day's card and cash
        # with no takings behind them: the very day the backfill refuses to
        # write and « Remplacer » prunes. One writer for every reading.
        stored = store_reading(export, self.stdout.write)
        self._report_unmatched(sorted(set(stored.sales.unmatched)))
        if job is not None:
            job.items_sold = export.total_quantity
            job.recorded = stored.sales.recorded
            job.unmatched = stored.unmatched

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
            'put the name in the base recipe\'s "Nom en happy hour sur la caisse" field.'
        )
