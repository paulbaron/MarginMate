"""Run a management command for ONE tenant.

    python manage.py tenant <folder> <command> [arguments…]
    python manage.py tenant k3v9x2m7q1ab reread_receipts --dry-run

The tenant is bound as `default` for the whole command - its database, and
its folders through accounts.paths - so the backfills, the purge, a
re-read… act on that bar and on no other. A business command run without
this finds no table at all: it fails loudly by construction.

Refused: the commands that act on the logins (they belong to the accounts
database, not a tenant), the tenancy commands themselves, and the ones
that would wipe or serve.
"""

import argparse

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Tenant
from accounts.tenancy import bound_tenant

REFUSED = frozenset(
    {
        "tenant",
        "migrate_tenants",
        "createsuperuser",
        "changepassword",
        "flush",
        "runserver",
        "testserver",
        # The production server serves every tenant, never one bound.
        "serve",
        "test",
    }
)


class Command(BaseCommand):
    help = "Lance une commande de gestion pour un seul espace : tenant <dossier> <commande> [arguments…]."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Le nom de dossier de l'espace.")
        parser.add_argument("command_name", help="La commande à lancer pour lui.")
        parser.add_argument("command_args", nargs=argparse.REMAINDER, help="Ses arguments.")

    def handle(self, *args, slug, command_name, command_args, **options):
        if command_name in REFUSED:
            raise CommandError(f"« {command_name} » ne se lance pas pour un espace.")
        tenant = Tenant.objects.filter(dir_name=slug).first()
        if tenant is None:
            raise CommandError(f"Aucun espace dans le dossier « {slug} ».")
        if not tenant.is_active:
            self.stderr.write(f"L'espace « {tenant.name} » est fermé : la commande le concerne quand même.")
        with bound_tenant(tenant):
            call_command(command_name, *command_args, stdout=self.stdout._out, stderr=self.stderr._out)
