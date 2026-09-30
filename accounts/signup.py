"""A signup: an invitation code, a bar's name, an address and a password
become a login, an espace and the login's place in it - all of it or none.

Three steps, in this order:

1. The code and the address are checked, reading only: a code refused (or
   an address that has a login) makes nothing at all - no espace copied and
   thrown away for every wrong code.
2. The espace's FILES are made (`provisioning.prepare_tenant`: its folder,
   its database copied from the template and migrated, the owner's
   integrations switched off) OUTSIDE any transaction on the accounts
   database. That takes seconds whenever the template is behind the code,
   and the accounts database's transactions are IMMEDIATE - SQLite's write
   lock from their first statement: made inside one, every login and
   session write of every bar waited for it. (`manage.py migrate_tenants`
   keeps the template current at every deploy, which keeps this step
   short.)
3. The ROWS, in ONE short transaction on the accounts database: the code
   and the address checked again (another signup may have taken either
   while the files were made), the invitation taken (an UPDATE that only an
   unused, unexpired invitation passes, so two signups racing on one code
   cannot both have it), the login created (username = the address,
   lower-cased; never staff, never superuser), the Tenant row, and the
   membership, as the espace's owner.

Anything failing after step 2 rolls the rows back and removes the espace's
folder: nothing half-made is left for a second try to trip on.

The code is looked at only here, once everything else typed was valid (the
page, accounts/pages.py): a page that said « code inconnu » beside a
password too short would answer, for free, whether a code exists. Unknown,
used and expired read the same.

**An address that has a login reads the same too** (security audit
ANON-5): « Un compte existe déjà avec cette adresse » answered, to anyone
holding one valid code, whether any address had an account - and the code
stayed unused for the next question. It is now `CODE_REFUSED`, on the code's
field, word for word; and each such refusal is counted on the invitation
(`Invitation.refused_addresses`, one conditional UPDATE), which is voided at
`TAKEN_ADDRESSES_BEFORE_VOID`: `used_at` set, nobody `used_by`, a warning
in the server's log. The invitee who typed the address he already signed up
with has two more tries; a code handed over cannot be spent asking.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from . import invitations, provisioning
from .models import Invitation, Membership
from .router import ACCOUNTS_ALIAS
from .users import normalize_email, users_for_email

logger = logging.getLogger(__name__)

#: Every refusal of a complete form: a code that opens nothing (unknown,
#: used, expired) and an address that already has a login, alike.
CODE_REFUSED = (
    "Ce code d'invitation n'ouvre pas d'inscription avec cette adresse e-mail : le code est inconnu, déjà utilisé "
    "ou expiré, ou l'adresse ne peut pas être inscrite. Déjà un compte ? Connectez-vous."
)
#: Said exactly as CODE_REFUSED: the page never tells whether an address
#: has an account.
EMAIL_TAKEN = CODE_REFUSED
#: Addresses with a login tried with one code before it is voided.
TAKEN_ADDRESSES_BEFORE_VOID = 3


class SignupRefused(Exception):
    """A signup that must not happen, said on one field of the form."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


class _AddressTaken(Exception):
    """Inside the rows' transaction: rolls it back, then counted (below)."""


def _address_taken(invitation: Invitation, now) -> SignupRefused:
    """Count a signup refused with `invitation`'s code for an address that
    has a login, void the invitation at the TAKEN_ADDRESSES_BEFORE_VOID-th,
    and return the refusal - the same as a code's."""
    with transaction.atomic(using=ACCOUNTS_ALIAS):
        Invitation.objects.filter(pk=invitation.pk, used_at__isnull=True).update(
            refused_addresses=F("refused_addresses") + 1
        )
        voided = Invitation.objects.filter(
            pk=invitation.pk, used_at__isnull=True, refused_addresses__gte=TAKEN_ADDRESSES_BEFORE_VOID
        ).update(used_at=now)
    if voided:
        logger.warning(
            "Invitation %s annulée : %s adresses qui ont déjà un compte essayées avec son code.",
            invitation.pk, TAKEN_ADDRESSES_BEFORE_VOID,
        )
    return SignupRefused("code", EMAIL_TAKEN)


def sign_up(*, code: str, bar_name: str, email: str, password: str, now=None):
    """(user, tenant) for a valid invitation, or SignupRefused. `password`
    has already passed the password validators (the form)."""
    now = now or timezone.now()
    email = normalize_email(email)

    # 1. Reading only: refused here, nothing is made.
    invitation = invitations.usable_invitation(code, now=now)
    if invitation is None:
        raise SignupRefused("code", CODE_REFUSED)
    if users_for_email(email).exists():
        raise _address_taken(invitation, now)

    # 2. The espace's files, outside the accounts database's transaction
    # (the module's docstring). It cleans up after itself when it fails.
    tenant = provisioning.prepare_tenant(bar_name)

    # 3. The rows, in one short transaction.
    try:
        with transaction.atomic(using=ACCOUNTS_ALIAS):
            if users_for_email(email).exists():
                # Taken by another signup while the files were made: rolled
                # back, then counted like any taken address.
                raise _AddressTaken
            # Taken only if still unused and unexpired: a signup racing on
            # the same code updates nothing and is refused like a used code.
            taken = (
                Invitation.objects.filter(pk=invitation.pk, used_at__isnull=True)
                .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
                .update(used_at=now)
            )
            if not taken:
                raise SignupRefused("code", CODE_REFUSED)
            user = get_user_model().objects.create_user(username=email, email=email, password=password)
            Invitation.objects.filter(pk=invitation.pk).update(used_by=user)
            tenant.save(force_insert=True)
            Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    except _AddressTaken:
        provisioning.remove_tenant_files(tenant)
        raise _address_taken(invitation, now) from None
    except IntegrityError:
        provisioning.remove_tenant_files(tenant)
        if users_for_email(email).exists():
            # Created in between by another signup for the same address.
            raise _address_taken(invitation, now) from None
        raise
    except BaseException:
        provisioning.remove_tenant_files(tenant)
        raise
    return user, tenant
