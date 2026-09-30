"""`manage.py running_jobs`: is anything running in the background, in any tenant?

    .venv\\Scripts\\python.exe manage.py running_jobs

A gather of invoices (and a source's « Tester »), a folder of tickets being
imported, an import of the till's sales: each is a thread of the server's
process, which stopping the server kills in the middle of what it writes.
deploy.cmd asks this command before it stops anything (DEPLOY.md,
section 10) and refuses while a job runs, naming it.

Every tenant the accounts database names is looked at, bound in turn
(accounts.tenancy.bound_tenant): its ScrapeJob, ReceiptBatch and
SalesImportJob rows still PENDING or RUNNING, with the tenant, what the job
is and when it was last heard from.

**Read-only.** A job nothing has been heard from for its STALE_AFTER
(common.JobLogMixin.is_stale: the server restarted under it) is listed as
presumed dead and does NOT count - the rule of transfer.runner.busy_reason,
which reaps such jobs first; this command writes nothing, the pages reap
them when they are drawn.

Exit code: 0 nothing runs, 1 something runs, 2 something could not be looked
at (an OPEN tenant's database that does not open, the accounts database
missing - never created: SQLite would make an empty file). A CLOSED tenant
(« actif » unticked) receives no request, so nothing is started in it: one
that cannot be looked at is a note, never a 2 - closing a tenant is what
serve and DEPLOY.md §11 tell the owner to do with a database that is gone, and a
2 made deploy.cmd refuse every deployment for it.
"""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils import timezone

from accounts import paths
from accounts.management.commands.serve import _is_memory
from accounts.models import Tenant
from accounts.router import ACCOUNTS_ALIAS
from accounts.tenancy import TenancyError, bound_tenant

RUNNING = 1
UNKNOWN = 2


def job_kinds():
    """(model, what one of its rows is called)."""
    from invoices.models import ReceiptBatch, ScrapeJob
    from recipes.models import SalesImportJob

    def scrape(job):
        return "test d'une source de factures" if job.kind == ScrapeJob.Kind.TEST else "récupération des factures"

    return (
        (ScrapeJob, scrape),
        (ReceiptBatch, lambda job: "import de tickets et factures"),
        (SalesImportJob, lambda job: "import des ventes de la caisse"),
    )


def ago(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 2 * 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


def moment(value) -> str:
    return timezone.localtime(value).strftime("%d/%m/%Y %H:%M:%S")


class Command(BaseCommand):
    help = (
        "Dit si une récupération de factures, un import de tickets ou un import des ventes tourne dans un espace "
        "(code de sortie 1), ou rien (0)."
    )
    requires_system_checks = []

    def handle(self, *args, **options):
        accounts_name = settings.DATABASES[ACCOUNTS_ALIAS]["NAME"]
        if not _is_memory(accounts_name) and not Path(str(accounts_name)).is_file():
            raise CommandError(
                "La base des comptes est introuvable (MARGINMATE_ACCOUNTS_DB) : impossible de savoir ce qui tourne.",
                returncode=UNKNOWN,
            )
        now = timezone.now()
        running, presumed_dead, problems, looked_at = [], [], [], 0
        for tenant in Tenant.objects.order_by("pk"):
            label = f"espace « {tenant.name} » ({tenant.dir_name})"
            try:
                database = paths.tenant_database(tenant)
            except ImproperlyConfigured:
                self._unseen(tenant, label, "son nom de dossier est invalide", problems)
                continue
            if not database.is_file() or database.stat().st_size == 0:
                # No database, no thread working in it.
                self.stdout.write(f"{label} : pas de base, rien ne peut y tourner.")
                continue
            try:
                with bound_tenant(tenant):
                    for model, kind in job_kinds():
                        active = model.objects.filter(status__in=[model.Status.PENDING, model.Status.RUNNING])
                        for job in active.order_by("started_at"):
                            heard = job.last_heartbeat or job.started_at
                            line = (
                                f"{label} : {kind(job)} ({job.get_status_display().lower()}), "
                                f"commencée le {moment(job.started_at)}, dernier signe de vie le {moment(heard)} "
                                f"(il y a {ago((now - heard).total_seconds())})"
                            )
                            (presumed_dead if job.is_stale else running).append(line)
            except (DatabaseError, TenancyError) as exc:
                self._unseen(tenant, label, f"sa base ne se lit pas ({exc})", problems)
                continue
            looked_at += 1

        for line in running:
            self.stdout.write(f"- EN COURS : {line}.")
        for line in presumed_dead:
            self.stdout.write(
                f"- sans nouvelles : {line} - considérée comme interrompue (le serveur a redémarré pendant qu'elle "
                "tournait), elle ne bloque rien."
            )
        for line in problems:
            self.stderr.write(f"- {line}")
        if running:
            raise CommandError(
                f"{len(running)} travail(s) en cours : attendez la fin avant d'arrêter le serveur.",
                returncode=RUNNING,
            )
        if problems:
            raise CommandError(
                f"{len(problems)} espace(s) n'ont pas pu être vérifiés : impossible de dire si rien ne tourne.",
                returncode=UNKNOWN,
            )
        self.stdout.write(f"Aucun travail en cours ({looked_at} espace(s) vérifié(s)).")

    def _unseen(self, tenant, label, what, problems):
        """A tenant that could not be looked at: no answer when it is open,
        a note when it is closed."""
        if tenant.is_active:
            problems.append(f"{label} : {what}.")
        else:
            self.stdout.write(f"{label} : fermé, {what} - ignoré (un espace fermé ne reçoit aucune requête).")
