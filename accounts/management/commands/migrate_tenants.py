"""Migrate every database of the installation.

    python manage.py migrate_tenants
    python manage.py migrate_tenants --tenant <folder>

In order: the accounts database, the template a new tenant is copied from
(created when missing), then every tenant's database - each one BOUND as
`default` while its migrate runs, because the data migrations write through
`Model.objects` and would otherwise seed or rewrite another file. Run it at
every deploy, before serving: a tenant left on the old schema answers
« no such column » to its bar alone, and a template left behind makes every
signup migrate its new tenant itself - seconds on the signup page, where a
current template makes that migrate a no-op. One tenant failing does not
stop the others; the command says which and exits in error.

A CLOSED tenant (« actif » unticked) is looked at as serve looks at it
(serve.migration_problems asks the open ones only): no database is « fermé,
sans base - ignoré », and one that does not migrate is a warning, never a
failure. Closing a tenant is exactly what serve and DEPLOY.md §11 tell the
owner to do with a database that is gone - and deploy.cmd runs this command
after the merge, where a failure leaves the site down.

With --tenant, only that tenant's database.
"""

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from accounts import paths, provisioning
from accounts.models import Tenant
from accounts.router import ACCOUNTS_ALIAS


class Command(BaseCommand):
    help = "Migre la base des comptes, le modèle et la base de chaque espace."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", dest="slug", help="Seulement cet espace (son nom de dossier).")

    def handle(self, *args, slug=None, verbosity=1, **options):
        inner = max(verbosity - 1, 0)
        if slug:
            tenant = Tenant.objects.filter(dir_name=slug).first()
            if tenant is None:
                raise CommandError(f"Aucun espace dans le dossier « {slug} ».")
            tenants = [tenant]
        else:
            self.stdout.write("Base des comptes…")
            call_command(
                "migrate",
                database=ACCOUNTS_ALIAS,
                interactive=False,
                verbosity=inner,
                skip_checks=True,
                stdout=self.stdout,
            )
            self.stdout.write("Modèle des nouveaux espaces…")
            provisioning.migrate_template(verbosity=inner, stdout=self.stdout)
            tenants = list(Tenant.objects.order_by("pk"))

        failures = []
        for tenant in tenants:
            label = f"{tenant.name} ({tenant.dir_name})"
            try:
                database = paths.tenant_database(tenant)
            except ImproperlyConfigured:
                self._failed(tenant, label, "son nom de dossier est invalide", failures)
                continue
            if not database.is_file():
                if tenant.is_active:
                    failures.append(label)
                    self.stderr.write(f"{label} : base introuvable.")
                else:
                    self.stdout.write(f"{label} : fermé, sans base - ignoré.")
                continue
            try:
                provisioning.migrate_tenant(tenant, verbosity=inner, stdout=self.stdout)
            except Exception as exc:  # noqa: BLE001 - one tenant failing must not stop the others
                self._failed(tenant, label, str(exc), failures)
                continue
            self.stdout.write(f"{label} : à jour.")
        if failures:
            raise CommandError(f"{len(failures)} espace(s) non migré(s) : " + ", ".join(failures))

    def _failed(self, tenant, label, reason, failures):
        """An open tenant's failure fails the command; a closed one's is a
        warning - it receives no request until it is opened again, and serve
        then asks for its migrations."""
        if tenant.is_active:
            failures.append(label)
            self.stderr.write(f"{label} : {reason}")
        else:
            self.stderr.write(
                f"{label} : fermé, non migré ({reason}) - avertissement seulement : migrez-le avant de le rouvrir."
            )
