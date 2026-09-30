"""Rebuild the index of the employees' signing links.

    python manage.py tenant <folder> staff_index_links      one tenant
    python manage.py staff_index_links                      every tenant

The employee's public link (/personnel/signer/<token>/…) carries no tenant:
the accounts database indexes each link's hash under the tenant that issued
it (accounts/links.py), written as links are issued and renewed, removed as
their requests are deleted or purged. A database that arrived otherwise -
a tenant adopted from an existing file, a copy put back from « Données »'s
backups - holds requests the index does not know: their links answer « lien
inconnu ». This makes the index hold exactly the hashes each tenant's
requests hold (`signature_requests.index_links`), and says what it changed.
Nothing else is touched; running it twice changes nothing the second time.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.tenancy import TenancyError
from staff.management.tenants import for_each_tenant
from staff.signature_requests import index_links


class Command(BaseCommand):
    help = "Remet d'accord l'index des liens de signature des salariés avec les demandes de chaque espace."

    def handle(self, *args, **options):
        for_each_tenant(self, self._index)

    def _index(self) -> None:
        try:
            added, removed = index_links()
        except TenancyError as error:
            # A hash another tenant already holds: a database copied from
            # one tenant into another - for a person to look at.
            raise CommandError(f"Un lien de cet espace est déjà indexé pour un autre espace ({error}).") from None
        self.stdout.write(
            f"{added} lien{'s' if added > 1 else ''} ajouté{'s' if added > 1 else ''}, "
            f"{removed} retiré{'s' if removed > 1 else ''}."
        )
