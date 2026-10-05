"""« Identifiants » (/identifiants/): the logins and passwords the gathers
sign in with - the invoice mailbox, Metro, L'Addition and every customer
portal of Achats → Sources - typed here instead of into the .env file.

What is typed goes into the espace's encrypted store (accounts/vault.py),
never the database. The rules of the page (security review of 01/10/2026):

- **The espace's owner only, with his MarginMate password confirmed**
  (`tenancy.is_owner`, `sudo.password_confirmed`): a session left open on
  the bar's PC is not enough to reach the third-party accounts.
- **A password is never shown back**: its field is always empty
  (`render_value=False`), a placeholder says whether one is stored, left
  empty it is kept, « Effacer » removes it. Any name that is a password
  somewhere on the page is drawn as a password everywhere on it - a source
  naming another's password as its LOGIN printed that password in clear. A
  login is shown (the owner typed it, and it tells two accounts apart). A
  value found only in the .env is said as such, never printed.
- **A password goes where it was typed for, nowhere else.** A portal's
  values are recorded with the https host of its login page
  (`vault.save(bindings=)`, `invoices.models.portal_host`); the scraper
  types them on that site only. The mailbox's app password is recorded with
  its IMAP server, and changing the server or the address asks for the app
  password again in the same save: otherwise whoever changed the server
  received it at the next gather.
- **Only names a source offers are accepted**, never one of the
  application's own (`invoices.models.app_env_name`), never a pair two sites
  share or a name that is a login on one source and a password on another
  (`portal_accounts` lists those « à corriger » and offers no field).
- Values no account uses any more (a source deleted, renamed) are listed by
  name to be removed: kept, they stayed in the store for ever.

In every espace (04/10/2026; before, the owner's only): each bar's
connectors sign in with what it typed here. The .env's values - shown as
« Fichier .env », offered to a portal, read as a fallback - are the
server's, and exist on the owner's page only
(`accounts.tenancy.server_accounts_allowed`): another bar's page never reads
the file, and its connectors never fall back on it (`vault.server_setting`).
Another bar's page holds the accounts its connectors use - the mailbox and
L'Addition - and neither Metro nor a portal, which are the owner's alone
(invoices/integrations.py); its fields are named after their account
(`FIELD_ALIASES`), so no server variable's name reaches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from . import sudo, vault
from .tenancy import is_owner, server_accounts_allowed

NOT_OWNER = "Seul le propriétaire de l'espace peut voir et modifier les identifiants des comptes."
CLEAR_SUFFIX = "__clear"
START_OVER = "tout_ressaisir"

STORED = "stored"
ENV = "env"
ENV_UNCONFIRMED = "env-unconfirmed"
MISSING = "missing"
MOVED = "moved"
STATUS_LABELS = {
    STORED: "Enregistré ici",
    ENV: "Fichier .env",
    ENV_UNCONFIRMED: "Fichier .env : site à confirmer",
    MISSING: "À renseigner",
    MOVED: "À ressaisir : le site a changé",
}
#: The statuses a gather cannot sign in with.
NOT_READY = {MISSING, MOVED, ENV_UNCONFIRMED}
HOST_FIELD = "host__"
SITE_FIELD = "site_env__"
SITE_CHANGED = (
    "Le site de « {title} » a changé depuis l'ouverture de cette page (maintenant : {host}) : rien n'a été "
    "enregistré. Vérifiez la source, puis saisissez de nouveau."
)
BUSY_PAGE = "Les identifiants sont momentanément inaccessibles : rechargez la page dans un instant."
DRAWN_FIELD = "drawn"
STATE_CHANGED = (
    "Les identifiants ont changé depuis l'ouverture de cette page (un autre onglet, ou le serveur ne pouvait pas "
    "les lire) : rien n'a été enregistré. Rechargez la page, puis saisissez de nouveau."
)
STARTED_OVER = (
    "Identifiants illisibles effacés. Saisissez-les de nouveau ; pour un espace client dont les identifiants sont "
    "dans le fichier .env, cochez de nouveau sa case."
)
#: STARTED_OVER on another bar's page, which has no .env and no portal.
STARTED_OVER_HOSTED = "Identifiants illisibles effacés. Saisissez-les de nouveau."
#: The .env names the application still reads for the mailbox when the
#: current ones are absent (config/settings.py): a line under the old name
#: is the same password in clear.
LEGACY_ENV_NAMES = {
    "INVOICE_EMAIL_ADDRESS": ("UBA_EMAIL_ADDRESS",),
    "INVOICE_EMAIL_APP_PASSWORD": ("UBA_EMAIL_APP_PASSWORD",),
}

MAILBOX_PASSWORD = "INVOICE_EMAIL_APP_PASSWORD"
MAILBOX_ADDRESS = "INVOICE_EMAIL_ADDRESS"
MAILBOX_HOST = "INVOICE_IMAP_HOST"
DEFAULT_IMAP_HOST = "imap.gmail.com"

#: A plain DNS name: letters, digits and dashes in dotted labels, at least
#: one dot, no port, no IP literal, no « localhost ».
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")
BAD_HOST = "Un nom de serveur, comme imap.gmail.com : sans « https:// », sans port, sans adresse IP."
HOST_NEEDS_PASSWORD = (
    "Changer le serveur ou l'adresse de la boîte mail demande de ressaisir son mot de passe d'application dans le "
    "même enregistrement : il ne doit partir que vers le serveur pour lequel il a été saisi."
)

#: The attributes that keep password managers from saving or filling these
#: fields (they are other sites' passwords, not MarginMate's).
NO_PASSWORD_MANAGER = {
    "data-1p-ignore": "true",
    "data-lpignore": "true",
    "data-bwignore": "true",
    "data-form-type": "other",
}


@dataclass
class Credential:
    name: str
    label: str
    secret: bool = False
    optional: bool = False
    placeholder: str = ""
    input_type: str = "text"


@dataclass
class Account:
    key: str
    title: str
    description: str
    credentials: list[Credential]
    #: The sources a portal account is for (Achats → Sources), as links.
    sources: list = field(default_factory=list)
    #: The https host a portal account's values are bound to ("" for the
    #: application's own accounts, whose sites are in the code).
    host: str = ""


FIXED_ACCOUNTS = [
    Account(
        "mailbox",
        "Boîte mail des factures",
        "La boîte où arrivent les factures envoyées par e-mail et les bons de consignes. Utilisez un « mot de "
        "passe d'application » (Gmail : Compte Google → Sécurité → Mots de passe des applications), jamais le mot "
        "de passe habituel du compte.",
        [
            Credential(MAILBOX_ADDRESS, "Adresse e-mail", input_type="email"),
            Credential(MAILBOX_PASSWORD, "Mot de passe d'application", secret=True),
            Credential(MAILBOX_HOST, "Serveur IMAP", optional=True, placeholder=DEFAULT_IMAP_HOST),
        ],
    ),
    Account(
        "metro",
        "Metro",
        "Le compte docs.metro.fr d'où sont téléchargées les factures Metro.",
        [
            Credential("METRO_EMAIL", "Identifiant (e-mail)", input_type="email"),
            Credential("METRO_PASSWORD", "Mot de passe", secret=True),
        ],
    ),
    Account(
        "laddition",
        "L'Addition (caisse)",
        "Le compte L'Addition Reporting d'où sont récupérées les ventes (Recettes & ventes › Ventes). Une autre "
        "caisse : importez son export sur la même page, sans identifiant.",
        [
            Credential("LADDITION_EMAIL", "Identifiant (e-mail)", input_type="email"),
            Credential("LADDITION_PASSWORD", "Mot de passe", secret=True),
        ],
    ),
]


#: The accounts another bar's page offers: its connectors' own. Metro and
#: the portals are the platform owner's alone (invoices/integrations.py).
HOSTED_ACCOUNT_KEYS = ("mailbox", "laddition")

#: A credential's field on another bar's page: named after its account and
#: its role, never after the server's variable its value is stored under
#: (the store keeps the .env's names, so that every connector asks one
#: question). `field_alias` names any other.
FIELD_ALIASES = {
    MAILBOX_ADDRESS: "boite_adresse",
    MAILBOX_PASSWORD: "boite_mot_de_passe",
    MAILBOX_HOST: "boite_serveur",
    "LADDITION_EMAIL": "caisse_identifiant",
    "LADDITION_PASSWORD": "caisse_mot_de_passe",
    "METRO_EMAIL": "metro_identifiant",
    "METRO_PASSWORD": "metro_mot_de_passe",
}
#: How another bar's page names a value no account uses any more, in its
#: « Effacer » box: in words, never by the server's variable.
ALIAS_WORDS = {
    MAILBOX_ADDRESS: "l'adresse de la boîte mail",
    MAILBOX_PASSWORD: "le mot de passe de la boîte mail",
    MAILBOX_HOST: "le serveur de la boîte mail",
    "LADDITION_EMAIL": "l'identifiant L'Addition",
    "LADDITION_PASSWORD": "le mot de passe L'Addition",
    "METRO_EMAIL": "l'identifiant Metro",
    "METRO_PASSWORD": "le mot de passe Metro",
}


def field_alias(name: str) -> str:
    """The HTML name of `name`'s field on another bar's page: its alias, its
    « Effacer » box's alias, the page's own fields as they are, and any other
    store name as a digest of it (a name the bar's own store holds)."""
    import hashlib

    if name.endswith(CLEAR_SUFFIX):
        return field_alias(name[: -len(CLEAR_SUFFIX)]) + CLEAR_SUFFIX
    if name in (DRAWN_FIELD, START_OVER) or name.startswith((HOST_FIELD, SITE_FIELD)):
        return name
    if name in FIELD_ALIASES:
        return FIELD_ALIASES[name]
    return "valeur_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def page_accounts(state: vault.VaultState):
    """(the accounts the page offers, the portals' report) for the bound
    espace: every fixed account and every portal's in the platform owner's,
    the mailbox and L'Addition in any other."""
    if server_accounts_allowed():
        report = portal_accounts(state.secret_names)
        return FIXED_ACCOUNTS + report.accounts, report
    return [account for account in FIXED_ACCOUNTS if account.key in HOSTED_ACCOUNT_KEYS], PortalReport([], [])


@dataclass
class PortalReport:
    accounts: list[Account]
    #: Sources whose names cannot be offered, with why (French, no value).
    to_fix: list[tuple[object, str]]


def portal_accounts(secret_names=frozenset()) -> PortalReport:
    """One account per pair of names the customer portals use - and the
    sources whose names are not offered, with why. `secret_names`: the
    names stored as passwords, which no source may use as its login."""
    from invoices.models import WebsiteInvoiceSource, app_env_name, portal_host

    sources = list(WebsiteInvoiceSource.objects.select_related("invoice_type").order_by("invoice_type__name", "pk"))
    logins = {source.username_env for source in sources}
    passwords = {source.password_env for source in sources}
    # The hosts every NAME is used for, not only every pair: two sources
    # sharing their password's name alone, on two sites, made one field
    # whose password was bound to whichever site came last.
    hosts_of_name: dict[str, set[str]] = {}
    for source in sources:
        for name in (source.username_env, source.password_env):
            hosts_of_name.setdefault(name, set()).add(portal_host(source.login_url))

    accounts: dict[tuple[str, str], Account] = {}
    to_fix = []
    for source in sources:
        pair = (source.username_env, source.password_env)
        host = portal_host(source.login_url)
        if not all(vault.allowed_name(name) and not app_env_name(name) for name in pair):
            to_fix.append((source.invoice_type, "ses noms sont ceux de l'application elle-même"))
            continue
        if pair[0] == pair[1] or pair[0] in passwords or pair[1] in logins:
            to_fix.append((source.invoice_type, "un même nom y sert d'identifiant et de mot de passe"))
            continue
        if pair[0] in secret_names:
            to_fix.append((source.invoice_type, "son identifiant porte le nom d'un mot de passe enregistré"))
            continue
        if not host:
            to_fix.append((source.invoice_type, "sa page de connexion n'est pas une adresse https"))
            continue
        if any(len(hosts_of_name[name]) > 1 for name in pair):
            to_fix.append((source.invoice_type, "ses noms sont aussi ceux d'une source d'un autre site"))
            continue
        account = accounts.get(pair)
        if account is None:
            account = accounts[pair] = Account(
                f"portal-{source.pk}",
                str(source.invoice_type.name),
                f"Espace client : {host}",
                [
                    Credential(source.username_env, "Identifiant"),
                    Credential(source.password_env, "Mot de passe", secret=True),
                ],
                host=host,
            )
        else:
            account.title = f"{account.title}, {source.invoice_type.name}"
        account.sources.append(source.invoice_type)
    return PortalReport(list(accounts.values()), to_fix)


def _env_file() -> Path:
    return Path(settings.BASE_DIR) / ".env"


@sensitive_variables("values")
def _env_values() -> set[str]:
    """The names the .env file holds a value for - read for the status only,
    the values themselves never leave this function."""
    path = _env_file()
    # Another bar's page reads nothing of the server's file: none of its
    # connectors would use it (`vault.server_setting`).
    if not server_accounts_allowed() or not path.is_file():
        return set()
    from dotenv import dotenv_values

    values = dotenv_values(path)
    return {key for key, value in values.items() if value}


def env_lines(name: str, env_names: set[str]) -> list[str]:
    """The .env FILE's lines holding this credential - its own name and the
    older names the settings still read. The file, read now: not the
    settings, which keep a deleted line until the server restarts, nor a
    default (INVOICE_IMAP_HOST is always set)."""
    return [line for line in (name, *LEGACY_ENV_NAMES.get(name, ())) if line in env_names]


def _in_env(name: str, env_names: set[str]) -> bool:
    return bool(env_lines(name, env_names))


def state_digest(state: vault.VaultState) -> str:
    """What the page was drawn from, as a keyed digest (never the values):
    the stored names, the logins, the bindings, whether it opened. A post
    drawn from another state saves nothing (`STATE_CHANGED`)."""
    import json

    from django.utils.crypto import salted_hmac

    logins = {name: value for name, value in state.values.items() if name not in state.secret_names}
    summary = json.dumps(
        [sorted(state.values), logins, state.bindings, state.env_bindings, sorted(state.secret_names), state.problem],
        sort_keys=True,
    )
    return salted_hmac("marginmate.accounts.credentials.drawn", summary, algorithm="sha256").hexdigest()


def mailbox_host(stored: dict[str, str]) -> str:
    """The IMAP server the mailbox's values go to, as the connector reads it."""
    return (stored.get(MAILBOX_HOST) or vault.server_setting(MAILBOX_HOST) or DEFAULT_IMAP_HOST).lower()


@dataclass
class Save:
    """What a valid post writes (vault.save's arguments)."""

    changes: dict[str, str | None]
    bindings: dict[str, str]
    env_bindings: dict[str, str]
    secret_names: set[str]

    def __bool__(self) -> bool:
        return bool(self.changes or self.env_bindings)


class CredentialsForm(forms.Form):
    """One field per credential, named after it; a password gets its
    « Effacer » box beside it, and so does every value no account uses any
    more (`orphans`). Each portal account also posts the host it was drawn
    with (`host__<key>`), and, while its values come from the .env, a box
    confirming that site (`site_env__<key>`)."""

    def __init__(
        self, accounts: list[Account], state: vault.VaultState, env_names: set[str], *args, aliased=False, **kwargs
    ):
        # Set before the fields are made: `add_prefix` names them.
        self.aliased = aliased
        super().__init__(*args, **kwargs)
        self.accounts = accounts
        self.state = state
        self.env_names = env_names
        stored = state.values
        offered = {c.name for a in accounts for c in a.credentials}
        # A value typed as a password stays one, whatever a source calls it.
        self.secret_names = {c.name for a in accounts for c in a.credentials if c.secret} | (
            set(state.secret_names) & offered
        )
        self.orphans = sorted(set(stored) - offered)
        self.host_of = {c.name: a.host for a in accounts for c in a.credentials if a.host}
        for account in accounts:
            for credential in account.credentials:
                if credential.name in self.fields:
                    continue
                secret = credential.name in self.secret_names
                attrs = {"autocomplete": "new-password" if secret else "off", "spellcheck": "false"}
                if secret:
                    attrs.update(NO_PASSWORD_MANAGER)
                    attrs["placeholder"] = (
                        "•••••••• enregistré - laisser vide pour le garder"
                        if credential.name in stored
                        else "Nouveau mot de passe"
                    )
                    widget = forms.PasswordInput(attrs=attrs, render_value=False)
                else:
                    if credential.placeholder:
                        attrs["placeholder"] = credential.placeholder
                    # A phone keyboard capitalising the first letter typed a
                    # login the site then echoes in lower case.
                    attrs.update({"autocapitalize": "none", "autocorrect": "off"})
                    widget = forms.TextInput(attrs={**attrs, "type": credential.input_type})
                self.fields[credential.name] = forms.CharField(
                    # A login name that is a password elsewhere is drawn as one.
                    label=credential.label if credential.secret or not secret else "Mot de passe",
                    required=False,
                    strip=not secret,
                    max_length=vault.MAX_VALUE_LENGTH,
                    widget=widget,
                    initial="" if secret else stored.get(credential.name, ""),
                )
                if secret:
                    self.fields[credential.name + CLEAR_SUFFIX] = forms.BooleanField(label="Effacer", required=False)
        for account in accounts:
            if not account.host:
                continue
            self.fields[HOST_FIELD + account.key] = forms.CharField(
                required=False, widget=forms.HiddenInput, initial=account.host
            )
            from_env = self.env_only(account)
            if from_env:
                self.fields[SITE_FIELD + account.key] = forms.BooleanField(
                    label=f"Le fichier .env contient ces identifiants : les envoyer à {account.host}",
                    required=False,
                    initial=self.env_confirmed(account),
                )
        for name in self.orphans:
            shown = ALIAS_WORDS.get(name, "une valeur enregistrée") if aliased else name
            self.fields[name + CLEAR_SUFFIX] = forms.BooleanField(label=f"Effacer {shown}", required=False)
        self.fields[DRAWN_FIELD] = forms.CharField(
            required=False, widget=forms.HiddenInput, initial=state_digest(state)
        )
        if state.unreadable:
            self.fields[START_OVER] = forms.BooleanField(
                label="Tout ressaisir : les identifiants illisibles seront effacés", required=False
            )

    def add_prefix(self, field_name):
        """A field's HTML name: the store's own name on the platform owner's
        page, its alias on any other (`FIELD_ALIASES`)."""
        if self.aliased:
            return field_alias(field_name)
        return super().add_prefix(field_name)

    def posted(self, name: str) -> bool:
        """Whether the post carried `name`'s field."""
        return self.add_prefix(name) in self.data

    def env_only(self, account: Account) -> list[str]:
        """The account's names whose value comes from the .env (none stored)."""
        return [
            c.name for c in account.credentials if c.name not in self.state.values and _in_env(c.name, self.env_names)
        ]

    def env_confirmed(self, account: Account) -> bool:
        names = self.env_only(account)
        return bool(names) and all(self.state.env_bindings.get(name) == account.host for name in names)

    def _touches(self, account: Account) -> bool:
        """Whether this post changes anything of a portal account."""
        stored = self.state.values
        for credential in account.credentials:
            name = credential.name
            if self.cleaned_data.get(name + CLEAR_SUFFIX):
                return True
            if not self.posted(name):
                continue
            typed = self.cleaned_data.get(name) or ""
            if name in self.secret_names:
                if typed:
                    return True
            elif typed != stored.get(name, ""):
                return True
        site = SITE_FIELD + account.key
        return site in self.fields and bool(self.cleaned_data.get(site)) != self.env_confirmed(account)

    def clean_INVOICE_IMAP_HOST(self):
        host = (self.cleaned_data.get(MAILBOX_HOST) or "").strip().lower().rstrip(".")
        if host and (not HOST_RE.match(host) or host.startswith("localhost")):
            raise forms.ValidationError(BAD_HOST)
        if host and not server_accounts_allowed():
            # Another bar's server is reached from the platform owner's
            # network: never a name only that network answers to (the
            # gather checks the addresses too, invoices/scrapers/egress.py).
            from invoices.scrapers import egress

            if egress.local_name(host):
                raise forms.ValidationError(egress.LOCAL_NAME.format(host=host))
        return host

    @sensitive_variables("typed", "cleaned")
    def clean(self):
        cleaned = super().clean()
        if MAILBOX_HOST in self.fields and MAILBOX_HOST not in self.errors:
            stored = self.state.values
            before_host = mailbox_host(stored)
            after = dict(stored)
            for name in (MAILBOX_HOST, MAILBOX_ADDRESS):
                if self.posted(name):
                    after[name] = cleaned.get(name) or ""
            changed = mailbox_host(after) != before_host or (after.get(MAILBOX_ADDRESS) or "") != (
                stored.get(MAILBOX_ADDRESS) or ""
            )
            typed = cleaned.get(MAILBOX_PASSWORD) or ""
            clearing = cleaned.get(MAILBOX_PASSWORD + CLEAR_SUFFIX)
            # What the connector would send: the page's, else the settings'.
            has_password = bool(stored.get(MAILBOX_PASSWORD) or vault.server_setting(MAILBOX_PASSWORD))
            if changed and has_password and not typed and not clearing:
                self.add_error(MAILBOX_PASSWORD, HOST_NEEDS_PASSWORD)
        # The store changed while the page was open (another tab saved, or
        # it could not be read when the page was drawn - every login then
        # read blank, and saving would have removed them): nothing saved.
        if (self.data.get(self.add_prefix(DRAWN_FIELD)) or "") != state_digest(self.state):
            self.add_error(None, STATE_CHANGED)
        # A portal's site changed while the page was open (a source edited,
        # an archive imported): what was typed was for the site the page
        # showed, and is not bound to another one.
        for account in self.accounts:
            drawn_for = self.data.get(self.add_prefix(HOST_FIELD + account.key)) or ""
            if account.host and drawn_for != account.host and self._touches(account):
                self.add_error(None, SITE_CHANGED.format(title=account.title, host=account.host))
        return cleaned

    @sensitive_variables("typed", "changes")
    def to_save(self) -> Save:
        """What to write, and the hosts to record: a login as it is now
        (empty = removed), a password only when one was typed or « Effacer »
        ticked. A field the post did not carry changes nothing."""
        stored = self.state.values
        changes: dict[str, str | None] = {}
        for name in self.fields:
            if (
                name.endswith(CLEAR_SUFFIX)
                or name in (START_OVER, DRAWN_FIELD)
                or name.startswith((HOST_FIELD, SITE_FIELD))
            ):
                continue
            if name in self.secret_names and self.cleaned_data.get(name + CLEAR_SUFFIX):
                changes[name] = None
                continue
            if not self.posted(name):
                continue
            typed = self.cleaned_data.get(name) or ""
            if name in self.secret_names:
                if typed:
                    changes[name] = typed
            elif typed != stored.get(name, ""):
                changes[name] = typed or None
        for name in self.orphans:
            if self.cleaned_data.get(name + CLEAR_SUFFIX):
                changes[name] = None
        env_bindings: dict[str, str] = {}
        for account in self.accounts:
            site = SITE_FIELD + account.key
            if site not in self.fields:
                continue
            ticked = bool(self.cleaned_data.get(site))
            if ticked != self.env_confirmed(account):
                for name in self.env_only(account):
                    env_bindings[name] = account.host if ticked else ""
        # A portal's password typed here while its login is still the
        # .env's: the owner vouched for this site, the login goes there too
        # (typed alone, the password left the portal refused for its login).
        for account in self.accounts:
            if not account.host:
                continue
            login, password = (c.name for c in account.credentials)
            if changes.get(password) and login in self.env_only(account) and login not in changes:
                env_bindings.setdefault(login, account.host)
        secret = {name for name, new in changes.items() if new and name in self.secret_names}
        return Save(changes, self._bindings(changes), env_bindings, secret)

    def _bindings(self, changes: dict[str, str | None]) -> dict[str, str]:
        """The host each value typed now is for. A portal's value is bound
        to its login page's host; its stored login follows a password typed
        again for the same site (the owner just vouched for the account
        there) - never the other way round. The mailbox's app password is
        bound to the IMAP server it will be sent to."""
        stored = self.state.values
        bindings: dict[str, str] = {}
        for account in self.accounts:
            if not account.host:
                continue
            login, password = (c.name for c in account.credentials)
            if changes.get(password):
                bindings[password] = account.host
                if changes.get(login) or (login in stored and changes.get(login, "") is not None):
                    bindings[login] = account.host
            elif changes.get(login):
                bindings[login] = account.host
        if changes.get(MAILBOX_PASSWORD):
            after = dict(stored)
            for name, new in changes.items():
                if new:
                    after[name] = new
                else:
                    after.pop(name, None)
            bindings[MAILBOX_PASSWORD] = mailbox_host(after)
        return bindings

    def start_over(self) -> bool:
        return bool(self.cleaned_data.get(START_OVER))

    def sections(self) -> list[dict]:
        """The page's blocks: each account with its rows (field, clear box,
        status)."""
        env_names = self.env_names
        stored = self.state.values
        bound = self.state.bindings
        sections = []
        seen = set()
        for account in self.accounts:
            rows = []
            for credential in account.credentials:
                if credential.name in seen:
                    continue
                seen.add(credential.name)
                name = credential.name
                in_env = _in_env(name, env_names)
                if name in stored:
                    moved = bool(account.host) and bound.get(name, "") != account.host
                    status = MOVED if moved else STORED
                elif in_env:
                    unconfirmed = bool(account.host) and self.state.env_bindings.get(name) != account.host
                    status = ENV_UNCONFIRMED if unconfirmed else ENV
                else:
                    status = MISSING
                optional_missing = credential.optional and status == MISSING
                rows.append(
                    {
                        "field": self[name],
                        "html_name": self[name].html_name,
                        "clear": self[name + CLEAR_SUFFIX] if name in self.secret_names else None,
                        "status": "" if optional_missing else status,
                        "status_label": "Facultatif" if optional_missing else STATUS_LABELS[status],
                        "still_in_env": env_lines(name, env_names) if name in stored else [],
                        "name": name,
                    }
                )
            if rows:
                statuses = {row["status"] for row in rows if row["status"]}
                site = SITE_FIELD + account.key
                sections.append(
                    {
                        "account": account,
                        "rows": rows,
                        "complete": not statuses & NOT_READY,
                        "host_field": self[HOST_FIELD + account.key] if account.host else None,
                        "site": self[site] if site in self.fields else None,
                    }
                )
        return sections

    def orphan_rows(self) -> list[dict]:
        return [
            {"name": name, "html_name": self.add_prefix(name), "clear": self[name + CLEAR_SUFFIX]}
            for name in self.orphans
        ]


def _page(request, form, report, status=200):
    sections = form.sections()
    context = {
        "form": form,
        "sections": sections,
        "orphans": form.orphan_rows(),
        "to_fix": report.to_fix,
        "unreadable": form.state.unreadable,
        "has_portals": bool(report.accounts) or bool(report.to_fix),
        "still_in_env": [line for section in sections for row in section["rows"] for line in row["still_in_env"]],
        "development": settings.DEBUG,
        "server_accounts": server_accounts_allowed(),
        # Another bar's page: the portals are the platform owner's alone.
        "portals_refused": None if server_accounts_allowed() else _portals_refused(),
    }
    return render(request, "accounts/credentials.html", context, status=status)


def _portals_refused() -> str:
    from invoices import integrations

    return integrations.PORTALS


@sensitive_post_parameters()
@never_cache
def credentials_page(request):
    """Every espace's owner, his MarginMate password confirmed: each bar
    types here what its own connectors sign in with (since 04/10/2026)."""
    if not is_owner(request):
        return render(request, "accounts/credentials.html", {"refused": NOT_OWNER}, status=403)
    if not sudo.confirmed(request):
        return sudo.ask(request)
    sudo.refresh(request)
    return _credentials(request)


@sensitive_variables("changes", "form")
def _credentials(request):
    state = vault.load()
    if state.problem == vault.BUSY:
        # Drawn from an empty state, the logins would read as blank and the
        # next save would remove them: no form until the store can be read.
        return render(request, "accounts/credentials.html", {"refused": BUSY_PAGE}, status=503)
    accounts, report = page_accounts(state)
    env_names = _env_values()
    # Another bar's fields carry no server variable's name (FIELD_ALIASES).
    aliased = not server_accounts_allowed()
    if request.method != "POST":
        return _page(request, CredentialsForm(accounts, state, env_names, aliased=aliased), report)
    form = CredentialsForm(accounts, state, env_names, request.POST, aliased=aliased)
    if not form.is_valid():
        return _page(request, form, report, status=400)
    save = form.to_save()
    if not save and not form.start_over():
        messages.info(request, "Rien n'a changé.")
        return redirect("accounts:credentials")
    try:
        vault.save(
            save.changes,
            bindings=save.bindings,
            env_bindings=save.env_bindings,
            secret_names=save.secret_names,
            start_over=form.start_over(),
        )
    except vault.VaultError as refused:
        form.add_error(None, str(refused))
        return _page(request, form, report, status=400)
    removed_only = save.changes and not any(save.changes.values()) and not any(save.env_bindings.values())
    if not save:
        message = STARTED_OVER if server_accounts_allowed() else STARTED_OVER_HOSTED
    elif removed_only:
        message = "Identifiants effacés."
    else:
        message = "Identifiants enregistrés. Ils servent dès la prochaine récupération."
    messages.success(request, message)
    return redirect("accounts:credentials")
