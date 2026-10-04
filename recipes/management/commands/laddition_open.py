"""Open a L'Addition Reporting page in a real browser, signed in.

    python manage.py laddition_open
    python manage.py laddition_open --path /v2/z-digital --no-headless --keep-open 120

Exists so the connection can be proved on its own, before any report-specific
clicking is written: run it, watch it sign in and land on the page. With
--no-headless you can see exactly what it sees, which is how the download
steps for a given report get worked out in the first place.

It runs for the platform owner's espace only (`manage.py tenant <folder>
laddition_open`, accounts.tenancy.server_accounts_allowed): with
--no-headless it shows the till on the server's desktop, and the operator
does not browse a customer's till - every other espace's account is used by
its own sales import alone (recipes/integration.py).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts import paths
from accounts.tenancy import server_accounts_allowed
from recipes.integration import require_tenant_for_command
from recipes.pos.laddition_session import LadditionAuthError, laddition_session

#: Said to the operator who runs it for another espace than the owner's.
OWNER_ONLY = (
    "laddition_open ne s'ouvre que dans l'espace du propriétaire de la plateforme : la caisse d'un autre espace "
    "ne se consulte pas depuis le serveur."
)


class Command(BaseCommand):
    help = "Sign in to L'Addition Reporting and open a page (connection check)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default="/v2/shift-details", help="Reporting path to open.")
        parser.add_argument(
            "--download-dir",
            default=None,
            help="The browser's download folder (default: the espace's downloads folder).",
        )
        parser.add_argument("--no-headless", action="store_true", help="Show the browser window.")
        parser.add_argument(
            "--keep-open",
            type=int,
            default=0,
            metavar="SECONDS",
            help="Leave the browser open afterwards, to look around.",
        )

    def handle(self, *args, **options):
        require_tenant_for_command("laddition_open")
        if not server_accounts_allowed():
            raise CommandError(OWNER_ONLY)
        if options["no_headless"]:
            settings.SCRAPER_HEADLESS = False
        try:
            with laddition_session(
                options["download_dir"] or str(paths.downloads_dir()), path=options["path"], log=self.stdout.write
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
