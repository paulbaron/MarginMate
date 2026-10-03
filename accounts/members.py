"""« Accès des employés » (/acces-employes/): the owner invites his
employees and chooses, for each, the pages he opens (accounts/access.py) -
and « Votre accès » (/invitation/<token>/), where an invited employee
chooses his password.

**Inviting** makes, in one transaction on the accounts database, the
employee's login (username = his address, lower-cased; his name as typed;
NO usable password - nobody can log in with it before he chooses one; never
staff nor superuser), his membership (MEMBER, the areas ticked) and the
invitation, whose token is shown to the owner once, in the answer that made
it, as a link to send himself (SMS, a messaging app): only its hash is kept
(`hash_secret`), and the link is stored nowhere - not even in the session.
A link lasts `INVITATION_DAYS`; « Nouveau lien » writes another hash, and
the old link stops working. The owner never knows the password.

**The same link chooses a NEW password** for an active employee who forgot
his: his old one works until the link is used. Only for a login this espace
alone holds - never staff, never superuser, never a login with another
espace (`_may_reset`): the owner of one bar must not choose the password of
somebody's login in another. `accept` checks it again.

**An address that already has a login is refused** on the owner's form: two
logins behind one address would name nobody at the login page
(accounts/users.py), and a login lives in one espace today. That tells the
owner the address has an account somewhere - which the public signup never
says (accounts/signup.py, ANON-5): so it is counted per espace, logged, and
past `TAKEN_ADDRESSES_PER_DAY` the form stops checking addresses until the
next day. An address held by an invitation that expired unused is freed
first (`users.free_the_address`); one of the espace's own employees is said
as such, and counted as nothing.

**Every request of the owner's page needs his password confirmed**
(accounts/sudo.py), as « Identifiants » does - on GET too: a login made here
is a door into the bar, a PC left logged in for two weeks must not open one,
and a confirmation asked at the POST would throw away what was typed. The
page itself is the owner's: the gate refuses it to every employee
(accounts/access.py names no area for it), and the view asks again.

**Removing an employee** deletes his login (when it is in no other espace,
and is neither staff nor superuser; otherwise his membership only): every
session of his reads as nobody's at its next request, and goes to the login
page.

**Choosing the password** (public: the employee is not logged in) checks
Django's password validators, uses the invitation up with a DELETE only the
first request passes, logs him in and sends him to his first page. A browser
already logged in as somebody else is told it will be logged out.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from common import is_id

from . import limiter, sudo
from .access import AREAS, DEFAULT_AREAS, OWNER_ONLY_PAGES, Access, access_of
from .forms import EMAIL_INVALID, PASSWORDS_DIFFER, REQUIRED
from .models import MemberInvitation, Membership, hash_secret
from .pages import _in_french
from .router import ACCOUNTS_ALIAS
from .tenancy import is_owner
from .users import USERNAME_MAX_LENGTH, free_the_address, normalize_email, users_for_email

logger = logging.getLogger(__name__)

#: How long an invitation link opens « Votre accès ».
INVITATION_DAYS = 7
#: Addresses with a login of another espace one espace may try in a day
#: before the form stops checking addresses (the module's docstring).
TAKEN_ADDRESSES_PER_DAY = 3

NOT_OWNER = "Seul le propriétaire de l'espace choisit les accès de ses employés."
EMAIL_TAKEN = (
    "Cette adresse a déjà un compte MarginMate : invitez votre employé avec une autre adresse e-mail "
    "(un compte n'ouvre qu'un seul espace)."
)
ALREADY_HERE = "{name} a déjà un accès avec cette adresse."
TOO_MANY_TAKEN = (
    "Trop d'adresses qui ont déjà un compte ont été essayées aujourd'hui : réessayez demain, ou invitez votre "
    "employé avec une adresse qui n'a pas encore de compte."
)
NO_SUCH_EMPLOYEE = "Cet employé n'a plus d'accès : la page a été rechargée."
UNKNOWN_PAGE = "Page inconnue : rechargez la page, puis cochez de nouveau."
NO_RESET = (
    "Ce compte ne se gère pas d'ici (il sert aussi ailleurs) : son mot de passe ne peut pas être changé par un lien."
)
LINK_DEAD = "Ce lien a expiré, a déjà servi, ou un lien plus récent l'a remplacé."
#: Said under the link when it starts with this PC's own address: a phone
#: cannot open it.
LOCAL_LINK = (
    "Ce lien commence par l'adresse de cet ordinateur : il ne s'ouvrira pas sur le téléphone de {name}. "
    "Renseignez MARGINMATE_SITE_URL (l'adresse publique du site) dans le fichier .env, ou ouvrez cette page "
    "depuis l'adresse publique."
)


def _ordered(keys) -> list[str]:
    """The areas ticked, in AREAS' order (what is stored and shown)."""
    chosen = set(keys)
    return [area.key for area in AREAS if area.key in chosen]


def _name(user) -> str:
    return user.first_name or user.email or user.get_username()


class AreasField(forms.MultipleChoiceField):
    """The boxes of AREAS; unticked, a box sends nothing - none ticked is
    an employee who opens no page yet, allowed."""

    widget = forms.CheckboxSelectMultiple

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Pages ouvertes")
        # A page no box offers: a page drawn before an area went, or a post
        # made by hand. In French, as every word on screen (Django's is
        # English here).
        kwargs.setdefault("error_messages", {"invalid_choice": UNKNOWN_PAGE})
        super().__init__(choices=[(area.key, area.label) for area in AREAS], **kwargs)


class InviteForm(forms.Form):
    name = forms.CharField(
        label="Nom de l'employé",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
        error_messages={"required": REQUIRED},
    )
    email = forms.EmailField(
        label="Son adresse e-mail",
        max_length=USERNAME_MAX_LENGTH,
        help_text="Elle lui sert d'identifiant pour se connecter ; rien n'y est envoyé.",
        widget=forms.EmailInput(attrs={"autocomplete": "off"}),
        error_messages={"required": REQUIRED, "invalid": EMAIL_INVALID},
    )
    pages = AreasField(initial=list(DEFAULT_AREAS))

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])


class PagesForm(forms.Form):
    pages = AreasField()


class ChoosePasswordForm(forms.Form):
    """The invited employee's password, twice, through Django's validators
    (the signup's words)."""

    password1 = forms.CharField(
        label="Mot de passe",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}),
        error_messages={"required": REQUIRED},
    )
    password2 = forms.CharField(
        label="Le même mot de passe, une seconde fois",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": REQUIRED},
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # Built here: in the language active for the page (accounts/forms.py).
        self.fields["password1"].help_text = " ".join(password_validation.password_validators_help_texts())

    def clean(self):
        cleaned = super().clean()
        first, second = cleaned.get("password1"), cleaned.get("password2")
        if first and second and first != second:
            self.add_error("password2", PASSWORDS_DIFFER)
        elif first:
            try:
                password_validation.validate_password(first, user=self.user)
            except ValidationError as refusal:
                self.add_error("password1", refusal)
        return cleaned


# -- What the pages do -----------------------------------------------------------------------------------------------


class InviteRefused(Exception):
    """An invitation that must not be made, said on the address's field."""


def _new_token(membership, now) -> str:
    """A new link for `membership`'s invitation: its hash written over the
    old one (whose link then opens nothing), the token returned."""
    token = secrets.token_urlsafe(32)
    MemberInvitation.objects.update_or_create(
        membership=membership,
        defaults={
            "token_hash": hash_secret(token),
            "created_at": now,
            "expires_at": now + timedelta(days=INVITATION_DAYS),
        },
    )
    return token


def invite(tenant, *, name: str, email: str, pages, now=None) -> tuple[Membership, str]:
    """The employee's login, membership and invitation, all or none; the
    membership and the link's token. InviteRefused for an address that
    already has a login."""
    now = now or timezone.now()
    email = normalize_email(email)
    free_the_address(email, now)
    try:
        with transaction.atomic(using=ACCOUNTS_ALIAS):
            if users_for_email(email).exists():
                raise InviteRefused(EMAIL_TAKEN)
            # No password: an unusable one, which no login matches.
            user = get_user_model().objects.create_user(username=email, email=email, first_name=name)
            membership = Membership.objects.create(
                user=user, tenant=tenant, role=Membership.Role.MEMBER, pages=_ordered(pages)
            )
            token = _new_token(membership, now)
    except IntegrityError:
        # The same address made at the same moment by another request.
        if users_for_email(email).exists():
            raise InviteRefused(EMAIL_TAKEN) from None
        raise
    return membership, token


def _may_reset(user) -> bool:
    """Whether a link from this espace may choose `user`'s password: a login
    of this espace alone, nobody's admin."""
    return not (user.is_staff or user.is_superuser) and user.memberships.count() == 1


def renew(membership, now=None) -> str | None:
    """A new link: the invitation again, or a new password for an active
    employee - None for a login this espace may not reset (`_may_reset`)."""
    if not _may_reset(membership.user):
        return None
    return _new_token(membership, now or timezone.now())


def remove(membership) -> None:
    """The employee's access gone: his login with it, unless it has another
    espace or is staff or superuser - then the membership alone."""
    user = membership.user
    with transaction.atomic(using=ACCOUNTS_ALIAS):
        if user.is_staff or user.is_superuser or user.memberships.exclude(pk=membership.pk).exists():
            membership.delete()
        else:
            user.delete()


def usable_invitation(token: str, now=None):
    """The invitation `token` opens - unexpired, its espace open - or None."""
    if not isinstance(token, str) or not token or len(token) > 100:
        return None
    invitation = (
        MemberInvitation.objects.select_related("membership__user", "membership__tenant")
        .filter(token_hash=hash_secret(token))
        .first()
    )
    if invitation is None or not invitation.is_usable(now) or not invitation.membership.tenant.is_active:
        return None
    return invitation


def accept(invitation, password: str) -> bool:
    """The password set and the invitation used up - by the first request
    only (a DELETE filtered on the hash it was read with). False when
    another request used it, « Nouveau lien » replaced it meanwhile, or the
    login is no longer one this espace may set a password for."""
    user = invitation.membership.user
    with transaction.atomic(using=ACCOUNTS_ALIAS):
        used, _ = MemberInvitation.objects.filter(pk=invitation.pk, token_hash=invitation.token_hash).delete()
        if not used or not _may_reset(user):
            return False
        user.set_password(password)
        user.save(update_fields=["password"])
    return True


# -- The owner's page ------------------------------------------------------------------------------------------------


def _taken_key(tenant) -> str:
    return f"marginmate:members:taken:{tenant.pk}:{timezone.localdate():%Y%m%d}"


def _too_many_taken(tenant) -> bool:
    return cache.get(_taken_key(tenant), 0) >= TAKEN_ADDRESSES_PER_DAY


def _count_taken(request) -> None:
    key = _taken_key(request.tenant)
    cache.add(key, 0, timeout=2 * 24 * 60 * 60)
    tried = cache.incr(key)
    logger.warning(
        "Accès des employés : l'espace %s (compte %s) a invité une adresse qui a déjà un compte (%s aujourd'hui).",
        request.tenant.pk,
        request.user.pk,
        tried,
    )


def _opened_sentence(count: int) -> str:
    if not count:
        return "aucune page ouverte."
    return f"{count} page ouverte." if count == 1 else f"{count} pages ouvertes."


def _link(request, token: str) -> str:
    from staff.signature_requests import absolute_link

    return absolute_link(reverse("accounts:member_invitation", args=[token]), request.build_absolute_uri)


def _is_local(link: str) -> bool:
    """Whether `link` names this PC or the local network: no phone outside
    it can open it."""
    host = urlsplit(link).hostname or ""
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _new_link(request, membership, token: str, *, reset: bool) -> dict:
    link = _link(request, token)
    name = _name(membership.user)
    return {
        "membership": membership.pk,
        "link": link,
        "name": name,
        "reset": reset,
        "local": LOCAL_LINK.format(name=name) if _is_local(link) else "",
    }


def _member(request):
    """The employee a POST names (`member`): a MEMBER of the bound espace,
    never an owner, never another espace's; None otherwise."""
    value = request.POST.get("member", "")
    if not is_id(value):
        return None
    return (
        Membership.objects.select_related("user")
        .filter(pk=int(value), tenant_id=request.tenant.pk, role=Membership.Role.MEMBER)
        .first()
    )


def _cards(request, refused_pages=None):
    """One card per employee of the espace, the oldest first, each with its
    boxes - drawn as posted for the one whose save was refused."""
    members = (
        Membership.objects.filter(tenant_id=request.tenant.pk, role=Membership.Role.MEMBER)
        .select_related("user", "invitation")
        .order_by("created_at", "pk")
    )
    now = timezone.now()
    cards = []
    for membership in members:
        user = membership.user
        invitation = getattr(membership, "invitation", None)
        active = user.has_usable_password()
        refused = refused_pages is not None and refused_pages[0] == membership.pk
        cards.append(
            {
                "membership": membership,
                "user": user,
                "name": _name(user),
                "active": active,
                # Never accepted: the invitation is still the way in - if
                # there is one (deleted in the admin, or a membership made
                # there: no link runs).
                "pending": not active,
                "no_link": not active and invitation is None,
                "expired": invitation is not None and not invitation.is_usable(now),
                # An active employee's link to choose a new password.
                "resetting": active and invitation is not None and invitation.is_usable(now),
                "invitation": invitation,
                "may_reset": _may_reset(user),
                "form": refused_pages[1] if refused else PagesForm(initial={"pages": _ordered(membership.pages or [])}),
                "opened": [area for area in AREAS if area.key in (membership.pages or [])],
            }
        )
    return cards


def _page(request, *, invite_form=None, refused_pages=None, new_link=None, status=200):
    context = {
        "cards": _cards(request, refused_pages),
        "invite_form": invite_form or InviteForm(),
        "new_link": new_link,
        "areas": AREAS,
        "owner_only": OWNER_ONLY_PAGES,
        "invitation_days": INVITATION_DAYS,
    }
    return render(request, "accounts/members.html", context, status=status)


def _invite(request):
    form = InviteForm(request.POST)
    if not form.is_valid():
        return _page(request, invite_form=form, status=400)
    data = form.cleaned_data
    here = (
        Membership.objects.select_related("user")
        .filter(tenant_id=request.tenant.pk, user__in=users_for_email(data["email"]))
        .first()
    )
    if here is not None:
        # One of this espace's own logins: said as such, nothing to count.
        form.add_error("email", ALREADY_HERE.format(name=_name(here.user)))
        return _page(request, invite_form=form, status=400)
    if _too_many_taken(request.tenant):
        # Not checked at all: no answer about the address.
        form.add_error("email", TOO_MANY_TAKEN)
        return _page(request, invite_form=form, status=429)
    try:
        membership, token = invite(request.tenant, name=data["name"], email=data["email"], pages=data["pages"])
    except InviteRefused as refusal:
        _count_taken(request)
        form.add_error("email", str(refusal))
        return _page(request, invite_form=form, status=400)
    messages.success(request, f"Accès créé pour {data['name']} : envoyez-lui le lien ci-dessous.")
    return _page(request, new_link=_new_link(request, membership, token, reset=False))


@never_cache
@require_http_methods(["GET", "POST"])
def members_page(request):
    if not is_owner(request):
        return render(request, "accounts/members.html", {"refused": NOT_OWNER}, status=403)
    if not sudo.confirmed(request):
        return sudo.ask(request)
    sudo.refresh(request)
    if request.method != "POST":
        return _page(request)

    action = request.POST.get("action")
    if action == "invite":
        return _invite(request)
    membership = _member(request)
    if membership is None:
        messages.warning(request, NO_SUCH_EMPLOYEE)
        return redirect("accounts:members")
    name = _name(membership.user)
    if action == "pages":
        form = PagesForm(request.POST)
        if not form.is_valid():
            return _page(request, refused_pages=(membership.pk, form), status=400)
        membership.pages = _ordered(form.cleaned_data["pages"])
        membership.save(update_fields=["pages"])
        messages.success(request, f"Accès de {name} enregistrés : {_opened_sentence(len(membership.pages))}")
        return redirect(reverse("accounts:members") + f"#employe-{membership.pk}")
    if action == "renew":
        token = renew(membership)
        if token is None:
            messages.error(request, NO_RESET)
            return redirect(reverse("accounts:members") + f"#employe-{membership.pk}")
        reset = membership.user.has_usable_password()
        if reset:
            messages.success(
                request,
                f"Lien pour un nouveau mot de passe de {name} : son mot de passe actuel "
                "reste valable tant que le lien n'a pas servi.",
            )
        else:
            messages.success(request, f"Nouveau lien d'invitation pour {name} : l'ancien ne fonctionne plus.")
        return _page(request, new_link=_new_link(request, membership, token, reset=reset))
    if action == "remove":
        remove(membership)
        messages.success(request, f"{name} n'a plus accès à l'espace.")
        return redirect("accounts:members")
    messages.warning(request, NO_SUCH_EMPLOYEE)
    return redirect("accounts:members")


def no_access(request):
    """An employee to whom no page is open yet (`Access.home_url`); anybody
    else goes to his first page."""
    access = access_of(request)
    if access.areas:
        return redirect(access.home_url)
    return render(request, "accounts/no_access.html")


# -- The employee's page ---------------------------------------------------------------------------------------------


def _hardened(response):
    """The link is a secret in the address: the page is kept by no browser
    and no search engine (as the employee's signing pages,
    staff/public_views.py)."""
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _site(request) -> str:
    """Where the employee comes back to: the site's own address."""
    from staff.signature_requests import absolute_link

    return absolute_link("/", request.build_absolute_uri).rstrip("/")


@login_not_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("password1", "password2")
@never_cache
@_in_french
def invitation_page(request, token):
    invitation = usable_invitation(token)
    if invitation is None or not _may_reset(invitation.membership.user):
        context = {"dead": LINK_DEAD, "logged_in": request.user.is_authenticated}
        return _hardened(render(request, "accounts/member_invitation.html", context, status=404))
    membership = invitation.membership
    user = membership.user
    reset = user.has_usable_password()
    form = ChoosePasswordForm(user, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        if not accept(invitation, form.cleaned_data["password1"]):
            context = {"dead": LINK_DEAD, "logged_in": request.user.is_authenticated}
            return _hardened(render(request, "accounts/member_invitation.html", context, status=404))
        auth_login(request, user)
        messages.success(
            request,
            f"Bienvenue dans l'espace « {membership.tenant.name} ». Pour revenir : {_site(request)}, "
            "avec votre adresse e-mail et ce mot de passe.",
        )
        home = Access.of(membership).home_url
        return limiter.remember_device(request, redirect(home), user.email)
    context = {
        "form": form,
        "tenant": membership.tenant,
        "invited": user,
        "reset": reset,
        "someone_else": request.user.is_authenticated and request.user.pk != user.pk,
    }
    status = 400 if form.is_bound else 200
    return _hardened(render(request, "accounts/member_invitation.html", context, status=status))
