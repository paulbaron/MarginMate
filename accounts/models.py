"""Logins, tenants and what links them - the CENTRAL rows.

One bar is one « espace » (`Tenant`), and one tenant is one SQLite database
of its own (`TENANTS_ROOT/<dir_name>/db.sqlite3`, accounts/paths.py) with
its files beside it. What has to be found BEFORE a tenant is chosen lives
here instead, in the accounts database (settings.DATABASES["accounts"],
accounts/router.py): who may log in, which tenant a login
belongs to, the invitation codes a signup needs, which tenant an
employee's public signing link belongs to (he is not logged in), and which
browsers receive a login's push notifications (`PushDevice`).

No business model points at any of these, and none of these at a business
model (accounts/tests/test_router.py checks it): the two sides live in
different files, and a foreign key cannot cross from one SQLite file to
another.
"""

import hashlib
from urllib.parse import urlsplit

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
    """One tenant: one bar's database and files. In the pages, « votre
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
    # The platform owner's tenant only: the server's own accounts - the
    # .env's Metro, mailbox and L'Addition values as a fallback -, Metro and
    # the supplier portals, the server's names on a page
    # (accounts.tenancy.server_accounts_allowed). Every other tenant runs the
    # mailbox and L'Addition with its own « Identifiants »
    # (integrations_allowed). Never set by a signup.
    uses_server_integrations = models.BooleanField(
        "utilise les accès du serveur",
        default=False,
        help_text="Metro, la boîte aux lettres, L'Addition et les portails du fichier .env.",
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
    """A login's place in a tenant. One per user today (the middleware
    takes the first); several later, without moving any data.

    An OWNER opens every page of the espace; a MEMBER - an employee - only
    the areas `pages` lists (accounts/access.py), which the owner ticks on
    « Accès des employés »."""

    class Role(models.TextChoices):
        OWNER = "owner", "Propriétaire"
        MEMBER = "member", "Employé"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField("rôle", max_length=10, choices=Role.choices, default=Role.OWNER)
    created_at = models.DateTimeField("depuis le", default=timezone.now)
    #: The keys of accounts.access.AREAS a MEMBER opens (an owner's are not
    #: read). A key no area has any more is ignored.
    pages = models.JSONField(
        "pages ouvertes",
        default=list,
        blank=True,
        help_text="Pour un employé : les pages que le propriétaire lui ouvre (accounts/access.py).",
    )

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


class MemberInvitation(models.Model):
    """The link an employee opens once to choose his password
    (/invitation/<token>/, accounts/members.py): his login and membership
    were made by the owner with it, the login with no usable password.

    Only the token's hash is kept (`hash_secret`): the link is shown to the
    owner once, when it is made. « Nouveau lien » writes another hash, which
    kills the old link; choosing the password deletes the row."""

    membership = models.OneToOneField(Membership, on_delete=models.CASCADE, related_name="invitation")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField("créée le", default=timezone.now)
    expires_at = models.DateTimeField("expire le")

    class Meta:
        verbose_name = "invitation d'employé"
        verbose_name_plural = "invitations d'employés"

    def __str__(self):
        return f"Invitation de {self.membership.user} - {self.membership.tenant}"

    def is_usable(self, now=None) -> bool:
        return (now or timezone.now()) < self.expires_at


class SigningLink(models.Model):
    """Which tenant an employee's public signing link belongs to.

    The link (/personnel/signer/<token>/…) carries no tenant and the
    employee is not logged in, so the page finds the tenant here by the
    token's hash (accounts/links.py), binds it, and then reads the request
    in that tenant's own database. Written when a link is issued or renewed
    (the old hash removed), removed only when its request is DELETED or
    PURGED: a cancelled, superseded or expired request keeps its link,
    whose page says so (410) - removed, it would say « lien inconnu »
    (404), which is not what happened. The index holds exactly
    the hashes the tenants' requests hold (adoption included)."""

    token_hash = models.CharField(max_length=64, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="signing_links")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "lien de signature"
        verbose_name_plural = "liens de signature"

    def __str__(self):
        return f"{self.token_hash[:8]}… - {self.tenant}"


class PushDevice(models.Model):
    """A browser that receives the espace's push notifications - a phone's
    Home Screen app, a computer's Chrome - for one login of one espace
    (notifications/, `/notifications/`).

    Central, beside the membership it belongs to: a logout (public, unbound)
    marks it, a membership deleted takes it along, and the scheduler reads
    every espace's devices from one place. `endpoint` is a bearer capability
    (whoever holds it can push to the phone) and `auth` a secret: neither is
    shown in the admin, logged whole, exported by « Données » or carried
    into data-dev (`accounts.deployment.purge_sessions` empties the table).

    Created or moved to another membership only by « Activer » (a gesture,
    notifications.devices.register); refreshed by the page's sync, never
    created by it. A push service answering 404/410 tombstones it
    (`gone_at`) rather than deleting it; a logout marks it (`logged_out_at`)
    until the same login syncs again. `server_key` is the
    applicationServerKey the browser says it subscribed with - never
    stamped by the server: a device subscribed under another SECRET_KEY is
    « à réactiver », never sent to."""

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="push_devices", verbose_name="membre"
    )
    endpoint = models.URLField("adresse de notification", max_length=2048, unique=True)
    # base64url: a point on P-256 (65 bytes) and the 16-byte auth secret,
    # both checked by notifications.webpush.check_keys before they are stored.
    p256dh = models.CharField(max_length=100)
    auth = models.CharField(max_length=40)
    server_key = models.CharField("clé du serveur", max_length=100)
    label = models.CharField("appareil", max_length=80)
    created_at = models.DateTimeField("inscrit le", default=timezone.now)
    seen_at = models.DateTimeField("vu le", default=timezone.now)
    last_success_at = models.DateTimeField("dernier envoi réussi", null=True, blank=True)
    last_error_at = models.DateTimeField("dernière erreur le", null=True, blank=True)
    gone_at = models.DateTimeField("désinscrit par le service le", null=True, blank=True)
    logged_out_at = models.DateTimeField("déconnecté le", null=True, blank=True)
    last_error = models.CharField("dernière erreur", max_length=200, blank=True)
    #: Consecutive failures, back to 0 at the next success.
    failures = models.PositiveIntegerField("échecs de suite", default=0)

    class Meta:
        verbose_name = "appareil"
        verbose_name_plural = "appareils"
        ordering = ["-seen_at", "-pk"]

    def __str__(self):
        return f"{self.label} - {self.membership_id}"

    @property
    def endpoint_host(self) -> str:
        """The push service's host - all of the endpoint a page or the
        admin ever shows."""
        try:
            return urlsplit(self.endpoint).hostname or ""
        except ValueError:
            return ""
