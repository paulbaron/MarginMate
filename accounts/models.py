"""Logins, espaces and what links them - the CENTRAL rows.

One bar is one « espace » (`Tenant`), and one espace is one SQLite database
of its own (`TENANTS_ROOT/<dir_name>/db.sqlite3`, accounts/paths.py) with
its files beside it. What has to be found BEFORE an espace is chosen lives
here instead, in the accounts database (settings.DATABASES["accounts"],
accounts/router.py): who may log in, which espace a login
belongs to, the invitation codes a signup needs, and which espace an
employee's public signing link belongs to (he is not logged in).

No business model points at any of these, and none of these at a business
model (accounts/tests/test_router.py checks it): the two sides live in
different files, and a foreign key cannot cross from one SQLite file to
another.
"""

import hashlib

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

#: A tenant's folder name: server-chosen, random, never the bar's name - it
#: appears in paths and must say nothing about the bar. Lower-case letters,
#: digits and dashes, starting with a letter or digit: `_template` (the
#: template database's folder, accounts/paths.py) can never be one.
DIR_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{2,39}$"


def hash_secret(value: str) -> str:
    """The SHA-256 of a secret that is shown once and never stored: an
    invitation code, an employee's signing token. Hex, 64 characters. A
    plain hash is enough for these: they are long random strings, not
    passwords a person chose."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Tenant(models.Model):
    """One espace: one bar's database and files. In the pages, « votre
    bar » / « espace » - never « workspace », which already names a
    navigation section in this codebase."""

    name = models.CharField("nom du bar", max_length=200)
    dir_name = models.CharField(
        "dossier",
        max_length=40,
        unique=True,
        validators=[RegexValidator(DIR_NAME_PATTERN)],
        help_text="Nom du dossier de l'espace sous TENANTS_ROOT : tiré au hasard, jamais le nom du bar.",
    )
    # The owner's espace only: Metro, the invoice mailbox, L'Addition, the
    # LLM parser and the portals' .env credentials are the owner's accounts
    # (accounts.tenancy.integrations_allowed). Never set by a signup.
    uses_server_integrations = models.BooleanField(
        "utilise les accès du serveur",
        default=False,
        help_text="Metro, la boîte aux lettres, L'Addition, l'analyse IA et les portails du fichier .env.",
    )
    created_at = models.DateTimeField("créé le", default=timezone.now)
    is_active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "espace"
        verbose_name_plural = "espaces"
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """A login's place in an espace. One per user today (the middleware
    takes the first); several later, without moving any data."""

    class Role(models.TextChoices):
        OWNER = "owner", "Propriétaire"
        MEMBER = "member", "Membre"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField("rôle", max_length=10, choices=Role.choices, default=Role.OWNER)
    created_at = models.DateTimeField("depuis le", default=timezone.now)

    class Meta:
        verbose_name = "membre"
        verbose_name_plural = "membres"
        constraints = [models.UniqueConstraint(fields=["user", "tenant"], name="accounts_one_membership_per_espace")]

    def __str__(self):
        return f"{self.user} - {self.tenant}"


class Invitation(models.Model):
    """A signup code the platform owner generated. Only its hash is kept
    (`hash_secret`); it is shown once, used once, and may expire."""

    code_hash = models.CharField(max_length=64, unique=True)
    note = models.CharField("note", max_length=200, blank=True)
    created_at = models.DateTimeField("créée le", default=timezone.now)
    expires_at = models.DateTimeField("expire le", null=True, blank=True)
    used_at = models.DateTimeField("utilisée le", null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="utilisée par",
    )
    #: Signups refused with this code for an address that already has a
    #: login. At accounts.signup.TAKEN_ADDRESSES_BEFORE_VOID the invitation
    #: is voided (`used_at` set, nobody `used_by`): a code is no way to ask
    #: which addresses have an account (security audit ANON-5).
    refused_addresses = models.PositiveSmallIntegerField(
        "adresses déjà inscrites essayées",
        default=0,
        help_text="Au 3e essai d'une adresse qui a déjà un compte, l'invitation est annulée.",
    )

    class Meta:
        verbose_name = "invitation"
        verbose_name_plural = "invitations"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.note or f"Invitation du {timezone.localtime(self.created_at):%d/%m/%Y}"

    @staticmethod
    def hash_code(code: str) -> str:
        return hash_secret(code.strip())

    def is_usable(self, now=None) -> bool:
        now = now or timezone.now()
        return self.used_at is None and (self.expires_at is None or now < self.expires_at)


class SigningLink(models.Model):
    """Which espace an employee's public signing link belongs to.

    The link (/personnel/signer/<token>/…) carries no espace and the
    employee is not logged in, so the page finds the espace here by the
    token's hash (accounts/links.py), binds it, and then reads the request
    in that espace's own database. Written when a link is issued or renewed
    (the old hash removed), removed only when its request is DELETED or
    PURGED: a cancelled, superseded or expired request keeps its link,
    whose page says so (410) - removed, it would say « lien inconnu »
    (404), which is not what happened. The index holds exactly
    the hashes the espaces' requests hold (adoption included)."""

    token_hash = models.CharField(max_length=64, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="signing_links")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "lien de signature"
        verbose_name_plural = "liens de signature"

    def __str__(self):
        return f"{self.token_hash[:8]}… - {self.tenant}"
