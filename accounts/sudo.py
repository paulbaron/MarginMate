"""« Confirmez votre mot de passe » - the MarginMate password asked again
before anything that reaches the third-party accounts: the « Identifiants »
page (accounts/credentials.py), and saving or testing a customer portal's
source (invoices/views.py), which decides where a stored password is typed.

A session alone is not enough there (security review of 01/10/2026): a bar's
PC stays logged in for two weeks, a cookie can be copied, and from either the
page could have changed where the mailbox's app password goes. The password
asked again is the login's own, checked through the login's limiter
(`limiter.LOGIN`: the same counters, so a guess here is a guess there), and
the session's key changes once it is confirmed.

The confirmation lasts `WINDOW_SECONDS` from the last protected request,
belongs to that login and that session only, and goes with the session at
logout; a login (the login page, the admin's) confirms too, having just
checked the password. A POST refused for want of it says that nothing was
saved (`EXPIRED_POST`): what was posted is never replayed. Stored in the
session as a time and the login's pk - never the password.
"""

from __future__ import annotations

import functools
import time
from urllib.parse import urlencode

from django import forms
from django.contrib.auth import update_session_auth_hash
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from common import local_path, safe_next

from . import limiter

SESSION_KEY = "_marginmate_sudo"
WINDOW_SECONDS = 15 * 60

WRONG_PASSWORD = "Mot de passe incorrect."
EXPIRED_POST = (
    "Votre mot de passe devait être confirmé de nouveau : rien n'a été enregistré. Une fois confirmé, refaites l'envoi."
)


def _now() -> float:
    return time.time()


def confirmed(request) -> bool:
    """Whether this login confirmed its password in this session within the
    window."""
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is None or not user.is_authenticated or session is None:
        return False
    stamp = session.get(SESSION_KEY)
    if not isinstance(stamp, dict) or stamp.get("user") != user.pk:
        return False
    until = stamp.get("until")
    return isinstance(until, (int, float)) and _now() < until


def stamp(request) -> None:
    """The password was just checked (here, or by a login): confirmed."""
    request.session[SESSION_KEY] = {"user": request.user.pk, "until": _now() + WINDOW_SECONDS}


_stamp = stamp


def refresh(request) -> None:
    """Keep a confirmation alive while a protected page is in use."""
    if confirmed(request):
        _stamp(request)


def forget(request) -> None:
    request.session.pop(SESSION_KEY, None)


def confirm_url(next_path: str) -> str:
    return f"{reverse('accounts:confirm_password')}?{urlencode({'next': next_path})}"


def ask(request, next_path: str | None = None):
    """The answer to a protected request made without a confirmation: the
    confirmation page, coming back to `next_path` (the page's own address
    for a POST - what was posted is not replayed). htmx: 401 and HX-Redirect,
    as a session that ended is answered."""
    target = confirm_url(local_path(request, next_path or request.get_full_path()) or "/")
    if request.method == "POST":
        # What was posted is not replayed: say so, after the confirmation.
        from django.contrib import messages

        messages.warning(request, EXPIRED_POST)
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=401)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


def password_confirmed(view):
    """The view runs only with a confirmation; it is then kept alive."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not confirmed(request):
            return ask(request)
        refresh(request)
        return view(request, *args, **kwargs)

    return wrapper


class ConfirmForm(forms.Form):
    password = forms.CharField(
        label="Mot de passe MarginMate",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
    )


def _page(request, form, back, status=200, refused=""):
    context = {"form": form, "next": back, "refused": refused}
    return render(request, "accounts/confirm_password.html", context, status=status)


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("password")
@never_cache
def confirm_password(request):
    back = safe_next(request, reverse("accounts:credentials"))
    if request.method != "POST":
        return _page(request, ConfirmForm(), back)
    username = request.user.get_username()
    # Counted before the password is looked at, as the login is.
    if not limiter.reserve(limiter.LOGIN, request, username):
        return _page(request, ConfirmForm(), back, status=429, refused=limiter.TOO_MANY)
    form = ConfirmForm(request.POST)
    if form.is_valid() and request.user.check_password(form.cleaned_data["password"]):
        limiter.succeeded(limiter.LOGIN, request, username)
        # A new session key: a cookie copied before the confirmation is not
        # a confirmed one. update_session_auth_hash cycles it AND stores the
        # password's hash again: check_password may just have upgraded the
        # hash (new hasher settings), and the session, still carrying the
        # old one, would be logged out on the next request.
        update_session_auth_hash(request, request.user)
        _stamp(request)
        return redirect(back)
    if form.is_valid():
        form.add_error("password", WRONG_PASSWORD)
    return _page(request, form, back, status=400)
