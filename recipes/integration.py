"""Whether this tenant may use the server's L'Addition account, and what it
reads where it may not.

The credentials (the « Identifiants » page, else LADDITION_EMAIL /
LADDITION_PASSWORD in .env) are the owner's own till. With one database per bar, they work in the owner's tenant
only (`accounts.tenancy.integrations_allowed`). Everywhere else the import is « à configurer », a later
step giving each tenant settings of its own.

One rule, checked wherever the account can be reached, because each place is
reachable on its own:

- the « Ventes » tab draws no form (`recipes/menu.py`), and the POST refuses
  (`views.trigger_sales_import`) - a page drawn before, or a crafted post;
- the thread refuses before anything is downloaded (`tasks`), since a
  thread's target can be called by anything;
- the session refuses before a browser starts or a password is read
  (`pos/laddition_session.py`), whoever opened it - `laddition_open` too;
- the commands refuse the download (`laddition_import`, `laddition_open`).

Reading exports already on disk uses no account: the backfills and
`laddition_import --file` read the tenant's own folder, or what the operator
names, and are not refused.

What a tenant that may not use the account is told is never the name of a
server variable: « X est absente du fichier .env » would say which names
exist on the server.
"""

from __future__ import annotations

from django.core.management.base import CommandError
from django.utils.text import capfirst

from accounts.tenancy import NoTenantBound, integrations_allowed, require_tenant, server_accounts_allowed

#: The till import where it is not the tenant's to use - as a clause, so a
#: page can put it after « Pour le combler : ». `refusal()` is the sentence.
TILL_TO_CONFIGURE = (
    "la récupération des ventes de la caisse (L'Addition) est à configurer — disponible prochainement "
    "dans les réglages de votre espace"
)
#: What fills the till's days a page finds without their money or their
#: means of payment, in an espace that runs no command on the server - as a
#: clause, after « Pour le combler : ». The owner's pages name the commands
#: instead (`till_commands_shown`).
TILL_REIMPORT = "récupérez ou importez de nouveau les ventes de la caisse sur ces jours (Recettes & ventes › Ventes)"


def till_allowed() -> bool:
    """Whether the server's L'Addition account may be used for the tenant
    this thread works for."""
    return integrations_allowed()


def till_commands_shown() -> bool:
    """Whether a page may hand this espace the till's server commands
    (`manage.py laddition_backfill_revenue`, `laddition_backfill_payments`):
    they run on the server and read its folders, so in the platform owner's
    espace only (`accounts.tenancy.server_accounts_allowed`). Every other
    espace is told to fetch or import its sales again (`TILL_REIMPORT`),
    never a command it cannot run nor a file name of the server."""
    return server_accounts_allowed()


def refusal() -> str:
    """TILL_TO_CONFIGURE as a sentence of its own."""
    return f"{capfirst(TILL_TO_CONFIGURE)}."


def require_tenant_for_command(command: str) -> None:
    """A command of the till's run for no tenant (multi mode, unbound): say
    how to run it, rather than a « no such table » from the empty database
    an unbound `default` is."""
    try:
        require_tenant()
    except NoTenantBound:
        raise CommandError(
            f"Aucun espace choisi : lancez-la pour un espace, « manage.py tenant <dossier> {command} … »."
        ) from None
