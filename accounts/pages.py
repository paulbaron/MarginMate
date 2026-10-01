"""The accounts' pages: « Connexion » (/connexion/), « Se déconnecter »
(/deconnexion/, a POST) and « Créer votre espace » (/inscription/).

All three are public (``@login_not_required``) and render outside any
tenant: they extend accounts/page.html, not base.html - no navigation, no
badges, nothing counted in anybody's database.

- The login asks for the e-mail address and the password (Django's
  LoginView and its form, accounts/forms.py). `next` is followed only to this
  site (Django's own check, `url_has_allowed_host_and_scheme`); anything else
  lands on the home page.
- The signup takes an invitation code, the bar's name, the address and the
  password twice, and does the rest all or nothing (accounts/signup.py: the
  tenant's files first, then the rows in one short transaction); then the
  new owner is logged in and lands in his empty tenant. The login page
  always offers it.
- Both count their attempts before checking anything (accounts/limiter.py)
  and, past the limit, say « réessayez plus tard » without checking
  anything - with a 429. The admin's own login counts on the login page's
  counters (accounts/forms.py, `LimitedAdminAuthenticationForm`). A login
  that succeeds - the signup's too - leaves the browser the « appareil
  connu » cookie for that address (`limiter.remember_device`): the
  address's ceiling, which a stranger can fill, never holds it back.
- The logout keeps what the browser remembers but the tenant's drafts,
  which static/js/ui.js forgets as « Se déconnecter » is sent.

The app is en-us and writes its French itself, but Django's own messages -
the password validators', a malformed address - speak for themselves: these
pages are rendered with French active (`_in_french`), rendering included.
"""

import functools
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render
from django.utils import translation
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from . import limiter, signup
from .forms import LoginForm, SignupForm
from .users import normalize_email

LOGGED_OUT = "Vous êtes déconnecté."


def _in_french(view):
    """Run and render `view` with French active. A TemplateResponse is
    rendered here, inside: its lazy messages are translated when rendered."""

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        with translation.override("fr"):
            response = view(request, *args, **kwargs)
            if callable(getattr(response, "render", None)) and not response.is_rendered:
                response.render()
        return response

    return wrapped


class LoginPage(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_redirect_url(self):
        """Django's (this site only), minus the login page itself: logged
        in, a `next` naming it made LoginView raise « Redirection loop » -
        a 500 from a link anyone can build."""
        url = super().get_redirect_url()
        if url and urlsplit(url).path == self.request.path:
            return ""
        return url

    def post(self, request, *args, **kwargs):
        email = normalize_email(request.POST.get("username"))
        # Counted before the password is looked at (a malformed POST too),
        # given back by a success (form_valid).
        if not limiter.reserve(limiter.LOGIN, request, email):
            # Nothing checked - not even the password: an unbound form.
            form = self.get_form_class()(request, initial={"username": email})
            context = self.get_context_data(form=form, refused=limiter.TOO_MANY)
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        from . import sudo

        limiter.succeeded(limiter.LOGIN, self.request, self.request.POST.get("username"))
        response = limiter.remember_device(self.request, super().form_valid(form))
        # The password was just checked: « Identifiants » does not ask it
        # again for its window (accounts/sudo.py).
        sudo.stamp(self.request)
        return response


class LogoutPage(LogoutView):
    """Django's logout (a POST), public: a login with no tenant left - the
    « aucun espace » page - must still be able to leave. The « appareil
    connu » cookie stays (accounts/limiter.py).

    Nothing asks the browser to empty its storage. The logout used to answer
    ``Clear-Site-Data: "storage"`` (security audit LOAD-2: an unsaved stock
    count's articles and quantities stayed readable from the public login
    page of a shared device), which emptied EVERYTHING the site kept in the
    browser: the gather's sources left unticked came back ticked - Metro, a
    portal asking for a code at every run - with the folds and the tables'
    sort, and nothing on screen said so (review of 29/09, LOGOUT-PREFS). The
    browser now forgets the tenant's DRAFTS alone: static/js/ui.js does it
    as a `form.topbar-logout` is sent (its `DRAFTS`) - base.html's, and the
    « indisponible » page's. A count never saved is still lost on an
    explicit logout: the price of a shared device."""

    def post(self, request, *args, **kwargs):
        was_in = request.user.is_authenticated
        response = super().post(request, *args, **kwargs)
        if was_in:
            messages.info(request, LOGGED_OUT)
        return response


login_view = login_not_required(_in_french(LoginPage.as_view()))
logout_view = login_not_required(LogoutPage.as_view())


@login_not_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("code", "password1", "password2")
@never_cache
@_in_french
def signup_view(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    refused, status = "", 200
    if request.method == "POST":
        email = normalize_email(request.POST.get("email"))
        form = SignupForm(request.POST)
        # An incomplete form is no guess: only a complete one is counted,
        # before its code is looked at, and a success gives it back.
        if limiter.blocked(limiter.SIGNUP, request, email) or (
            form.is_valid() and not limiter.reserve(limiter.SIGNUP, request, email)
        ):
            form = SignupForm(initial={"bar_name": request.POST.get("bar_name", ""), "email": email})
            refused, status = limiter.TOO_MANY, 429
        elif form.is_valid():
            data = form.cleaned_data
            try:
                user, tenant = signup.sign_up(
                    code=data["code"], bar_name=data["bar_name"], email=data["email"], password=data["password1"]
                )
            except signup.SignupRefused as refusal:
                # Counted already (the reservation above).
                form.add_error(refusal.field, refusal.message)
            else:
                limiter.succeeded(limiter.SIGNUP, request, email)
                auth_login(request, user)
                messages.success(request, f"Bienvenue : l'espace « {tenant.name} » est prêt.")
                # Logged in: this browser is a device the address logged in on.
                return limiter.remember_device(request, redirect(settings.LOGIN_REDIRECT_URL), data["email"])
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form, "refused": refused}, status=status)
