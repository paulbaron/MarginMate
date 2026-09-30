"""The login and signup forms (accounts/pages.py), and the admin's login
form (accounts/admin_site.py). French words of their own; Django's own
messages (a password too short, an address that is none) come out in French
because the pages are rendered with French active."""

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from . import limiter
from .users import USERNAME_MAX_LENGTH, normalize_email, user_for_email

REQUIRED = "Ce champ est obligatoire."
EMAIL_INVALID = "Saisissez une adresse e-mail valide."
PASSWORDS_DIFFER = "Les deux mots de passe ne sont pas identiques."


class LoginForm(AuthenticationForm):
    """Django's login form, asking for the e-mail address. The address is
    turned into the login's username (`users.user_for_email`); one that
    names nobody is checked as typed, and fails like a wrong password."""

    username = forms.EmailField(
        label="Adresse e-mail",
        max_length=254,
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
        error_messages={"required": REQUIRED, "invalid": EMAIL_INVALID},
    )
    password = forms.CharField(
        label="Mot de passe",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        error_messages={"required": REQUIRED},
    )

    error_messages = {
        "invalid_login": "Adresse e-mail ou mot de passe incorrect.",
        "inactive": "Ce compte est désactivé.",
    }

    def clean_username(self):
        email = normalize_email(self.cleaned_data["username"])
        user = user_for_email(email)
        return user.get_username() if user is not None else email


class LimitedAdminAuthenticationForm(AdminAuthenticationForm):
    """Django's admin login form, on the login page's counters
    (accounts/limiter.py, `LOGIN`). /admin/login/ is Django's own page and
    public: a second door the limiter did not see, where the platform's
    superuser - who reads every bar's logins - could be guessed at without
    any limit, and where a non-staff login's right password, re-hashed and
    saved before the staff refusal whenever its hash predates Django's
    iteration count, answered measurably slower than a wrong one.

    `self.request` is what the limiter judges, so a device the address
    logged in on (its « appareil connu » cookie) and the PC itself pass the
    address's ceiling here as on the login page. A success is marked on the
    request by `limiter.succeeded`: the admin's login answer carries the
    cookie once it goes through `limiter.remember_device`."""

    def clean(self):
        username = self.data.get("username")
        if not limiter.reserve(limiter.LOGIN, self.request, username):
            raise ValidationError(limiter.TOO_MANY, code="too_many")
        # A wrong password or a login that may not enter raises here, and
        # its reservation stays counted.
        cleaned = super().clean()
        if self.get_user() is not None:
            # Only a password checked and right is a success - not a form
            # missing its password, which would forget the address's count.
            limiter.succeeded(limiter.LOGIN, self.request, username)
        return cleaned


class SignupForm(forms.Form):
    """What a signup asks. The code is only required here: whether it opens
    anything is asked once everything else is valid (accounts/signup.py)."""

    code = forms.CharField(
        label="Code d'invitation",
        max_length=64,
        help_text="Le code reçu avec votre invitation, avec ou sans ses tirets.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"}),
        error_messages={"required": REQUIRED},
    )
    bar_name = forms.CharField(
        label="Nom du bar",
        max_length=200,
        help_text="Affiché en haut de chaque page de votre espace.",
        widget=forms.TextInput(attrs={"autocomplete": "organization"}),
        error_messages={"required": REQUIRED},
    )
    email = forms.EmailField(
        label="Adresse e-mail",
        max_length=USERNAME_MAX_LENGTH,
        help_text="Elle vous sert d'identifiant pour vous connecter.",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
        error_messages={"required": REQUIRED, "invalid": EMAIL_INVALID},
    )
    password1 = forms.CharField(
        label="Mot de passe",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": REQUIRED},
    )
    password2 = forms.CharField(
        label="Le même mot de passe, une seconde fois",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": REQUIRED},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Built here, not at import: in the language active for the page. The
        # validators' own sentences, one after the other - Django's HTML list
        # would sit inside the help's <p>, which a browser closes before it.
        self.fields["password1"].help_text = " ".join(password_validation.password_validators_help_texts())

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        first, second = cleaned.get("password1"), cleaned.get("password2")
        if first and second and first != second:
            self.add_error("password2", PASSWORDS_DIFFER)
        elif first:
            User = get_user_model()
            try:
                password_validation.validate_password(first, user=User(username=email or "", email=email or ""))
            except ValidationError as refusal:
                self.add_error("password1", refusal)
        return cleaned
