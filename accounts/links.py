"""Which espace an employee's public signing link belongs to.

The link (/personnel/signer/<token>/…) carries no espace, and the employee
is not logged in: the accounts database keeps
``SigningLink(token_hash, tenant)`` so the public page can find the espace
by the token's hash, bind it, and then read the request in that espace's
own database. Scanning every espace's database instead would cost one query
per bar on every hit and tell a stranger, by its timing, how many there are.

    register(token_hash)       a link was issued or renewed (bound, in the espace that issued it)
    forget(*token_hashes)      its request was deleted or purged, or its link renewed (the old hash)
    resolve(token_hash)        the Tenant to bind, or None → « lien inconnu »

A cancelled, superseded or expired request KEEPS its link in the index: its
page says « annulée », « corrigé depuis » or « expiré » (410); forgotten, it
would say « lien inconnu » (404), which is not what happened. The index holds exactly the hashes the espace's requests hold
(`staff.signature_requests.index_links` rebuilds it; adoption writes it so).

`register` may run inside the espace's own transaction: if that rolls back,
the index names a hash the espace does not have, and the page says « lien
inconnu » as it would anyway. `forget` should run once the espace's change
is committed (``transaction.on_commit(lambda: links.forget(h))``): the other
way round, a rollback would leave a live link that no longer opens.
"""

from __future__ import annotations

from .models import SigningLink
from .tenancy import TenancyError, require_tenant


def register(token_hash: str) -> None:
    if not token_hash:
        raise ValueError("A signing link is registered by its token's hash.")
    tenant = require_tenant()
    link, created = SigningLink.objects.get_or_create(token_hash=token_hash, defaults={"tenant": tenant})
    if not created and link.tenant_id != tenant.pk:
        raise TenancyError("This signing link already belongs to another espace.")


def forget(*token_hashes: str) -> int:
    """Remove the bound espace's links with these hashes (blank ones are
    skipped); returns how many went."""
    hashes = [token_hash for token_hash in token_hashes if token_hash]
    if not hashes:
        return 0
    deleted, _ = SigningLink.objects.filter(tenant=require_tenant(), token_hash__in=hashes).delete()
    return deleted


def resolve(token_hash: str):
    """The espace to bind for this hash: its active Tenant, or None
    (unknown, or the espace was closed)."""
    if not token_hash:
        return None
    link = SigningLink.objects.select_related("tenant").filter(token_hash=token_hash, tenant__is_active=True).first()
    return link.tenant if link else None
