"""The staff commands and the tenants (accounts/tenancy.py).

A command run through `manage.py tenant <folder> <command>` is bound to
that tenant and acts on it alone. Run on its own, nothing is bound - and a
staff command's work (the legally required purge above all) concerns EVERY
bar: `for_each_tenant` runs it once per tenant, each bound in turn, under a
heading naming it. Bound, it runs once.
"""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

from accounts.tenancy import TenancyError, bound_tenant, current_tenant


def for_each_tenant(command, work) -> None:
    """`work()` once - a tenant already bound - or, unbound, once for
    every tenant (closed ones too: what they still hold is still theirs to
    purge), each bound in turn. A tenant that cannot be opened (its
    database gone) or whose work is refused is said on stderr and the others
    still run; the command then ends in error naming them."""
    if current_tenant() is not None:
        work()
        return
    from accounts.models import Tenant

    tenants = list(Tenant.objects.order_by("name", "pk"))
    if not tenants:
        command.stdout.write("Aucun espace.")
        return
    failed = []
    for tenant in tenants:
        closed = "" if tenant.is_active else " (fermé)"
        command.stdout.write(f"Espace « {tenant.name} »{closed} ({tenant.dir_name}) :")
        try:
            with bound_tenant(tenant):
                work()
        except (TenancyError, ImproperlyConfigured, CommandError) as error:
            command.stderr.write(f"Espace « {tenant.name} » ({tenant.dir_name}) : {error}")
            failed.append(tenant)
    if failed:
        names = ", ".join(f"« {tenant.name} » ({tenant.dir_name})" for tenant in failed)
        raise CommandError(f"Pas fait pour {len(failed)} espace{'s' if len(failed) > 1 else ''} : {names}.")
