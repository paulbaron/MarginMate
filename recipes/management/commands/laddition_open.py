"""Open a L'Addition Reporting page in a real browser, signed in.

    python manage.py laddition_open
    python manage.py laddition_open --path /v2/z-digital --no-headless --keep-open 120

Exists so the connection can be proved on its own, before any report-specific
clicking is written: run it, watch it sign in and land on the page. With
--no-headless you can see exactly what it sees, which is how the download
steps for a given report get worked out in the first place.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from recipes.pos.laddition_session import LadditionAuthError, laddition_session


class Command(BaseCommand):
    help = "Sign in to L'Addition Reporting and open a page (connection check)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default="/v2/shift-details", help="Reporting path to open.")
        parser.add_argument("--download-dir", default=str(settings.SCRAPE_DOWNLOAD_DIR))
        parser.add_argument("--no-headless", action="store_true", help="Show the browser window.")
        parser.add_argument(
            "--keep-open", type=int, default=0, metavar="SECONDS",
            help="Leave the browser open afterwards, to look around.",
        )

    def handle(self, *args, **options):
        if options["no_headless"]:
            settings.SCRAPER_HEADLESS = False
        try:
            with laddition_session(
                options["download_dir"], path=options["path"], log=self.stdout.write
            ) as driver:
                self.stdout.write(self.style.SUCCESS(f"Connected. URL: {driver.current_url}"))
                self.stdout.write(f"Title: {driver.title}")
                body = driver.find_element("tag name", "body").text
                self.stdout.write("--- first 1500 characters of the page ---")
                self.stdout.write(body[:1500])
                if options["keep_open"]:
                    import time

                    self.stdout.write(f"Holding the browser open for {options['keep_open']}s...")
                    time.sleep(options["keep_open"])
        except LadditionAuthError as exc:
            raise CommandError(str(exc)) from exc
