"""Import sales from L'Addition for a date range.

    python manage.py laddition_import --from 2026-06-01 --to 2026-06-30
    python manage.py laddition_import --from 2023-12-01 --to 2026-09-02
    python manage.py laddition_import --file some-export.xlsx
    python manage.py laddition_import --from 2026-06-01 --to 2026-06-30 --dry-run

Downloads the "Lignes de ventes" export (splitting the range into windows
the back office will accept), reads it, and records the sales. Ranges longer
than two years are handled; --file skips the download and reads one already
downloaded.
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from recipes.pos.laddition_download import LadditionDownloadError, download_sales_lines
from recipes.pos.laddition_xlsx import LadditionExportError, parse_sales_exports
from recipes.pos.laddition_session import LadditionAuthError
from recipes.sales import record_sales


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
        parser.add_argument("--download-dir", default=str(settings.SCRAPE_DOWNLOAD_DIR))
        parser.add_argument("--no-headless", action="store_true", help="Show the browser.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Read and report, but write nothing - use it to check the names line up first.",
        )

    def handle(self, *args, **options):
        files = options["files"]
        if not files:
            if not options["start"] or not options["end"]:
                raise CommandError("Give --from and --to, or --file.")
            start, end = _as_date(options["start"]), _as_date(options["end"])
            if start > end:
                raise CommandError("--from is after --to.")
            if options["no_headless"]:
                settings.SCRAPER_HEADLESS = False
            try:
                files = download_sales_lines(
                    start, end, options["download_dir"], log=self.stdout.write
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

        result = record_sales(export.entries, source="laddition")
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded {result.recorded} recipe/day totals "
                f"({result.created} new, {result.updated} updated)."
            )
        )
        self._report_unmatched(sorted(set(result.unmatched)))

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
