"""Whether this tenant may fetch its sales from L'Addition, and what it reads
where it may not.

Every espace may (since 04/10/2026; before, the owner's only): each signs in
with the L'Addition account typed on ITS « Identifiants » page - the .env's
LADDITION_EMAIL / LADDITION_PASSWORD stand in for a value not typed in the
platform owner's espace only (`accounts.vault.server_setting`). Refused
unbound (`accounts.tenancy.integrations_allowed`). What stays the owner's:
`laddition_open`, which shows a till in a browser of the server, and the
names of the server's commands on a page (`till_commands_shown`).

One rule, checked wherever the account can be reached, because each place is
reachable on its own:

- the « Ventes » tab draws no form (`recipes/menu.py`), and the POST refuses
  (`views.trigger_sales_import`) - a page drawn before, or a crafted post;
- the thread refuses before anything is downloaded (`tasks`), since a
  thread's target can be called by anything;
- the session refuses before a browser starts or a password is read
  (`pos/laddition_session.py`), whoever opened it - `laddition_open` too;
- the commands refuse the download (`laddition_import`), and
  `laddition_open` runs for the platform owner's espace only.

Reading exports already on disk uses no account: the backfills and
`laddition_import --file` read the tenant's own folder, or what the operator
names, and are not refused.

What a tenant is told is never the name of a server variable: « X est
absente du fichier .env » would say which names exist on the server.
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


#: Another bar's sign-in asked for with no L'Addition account on its
#: « Identifiants »: said before the server's Chrome starts.
TILL_LOGIN_MISSING = "L'identifiant ou le mot de passe de L'Addition manque : renseignez-les sur la page Identifiants."
#: The names L'Addition signs in with (accounts/credentials.py).
TILL_LOGIN_NAMES = ("LADDITION_EMAIL", "LADDITION_PASSWORD")


def till_login_missing() -> bool:
    """Whether a session would start a browser only to find no account to
    type: outside the platform owner's espace (whose .env or page stands, as
    before), its « Identifiants » holds no L'Addition login and password
    (`vault.ready`, one reading of the store)."""
    from accounts import vault

    return not server_accounts_allowed() and not vault.ready(*TILL_LOGIN_NAMES)


def till_allowed() -> bool:
    """Whether the bound tenant may fetch its sales from L'Addition, with its
    own « Identifiants » (`integrations_allowed`: any bound tenant)."""
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
