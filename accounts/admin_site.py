"""The admin site, as the platform's owner's tool.

Installed in place of django.contrib.admin's own config
(config/settings.py INSTALLED_APPS, Django's documented way of replacing the
default site): every app's `admin.site.register` lands on this site.

Superusers only. Staff alone is not enough: the admin lists every login of
every bar (auth.User) and edits raw rows, and a signup never sets is_staff
anyway. The business models it shows are the superuser's own tenant's - the
request is bound to it like any page (accounts.middleware.TenantMiddleware).
Its login page, Django's and public, counts its attempts on the login
page's counters (accounts/limiter.py), and a login that succeeds there
leaves the « appareil connu » cookie as the login page's does. Its logout is
Django's own.

**Every page but the login and the logout asks for the MarginMate password
again** (accounts/sudo.py, security review of 01/10/2026): the admin changes
any login's password and any membership's role, and edits raw rows - a
superuser's session left open on the bar's PC, or a copied cookie, reached
all of it. `admin_view` wraps every view Django's site and every
ModelAdmin route through it (the user's password page included); the
login is not one of them, and the logout is let through. A request with no
confirmation is sent to « Confirmez votre mot de passe », coming back to the
admin page asked for (a POST is not replayed: nothing it posted is saved);
a page in use keeps the confirmation alive.
"""

import functools

from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache


class MarginMateAdminSite(admin.AdminSite):
    def has_permission(self, request):
        user = request.user
        return bool(user.is_active and user.is_superuser)

    def admin_view(self, view, cacheable=False):
        """Django's, the view behind the password confirmation. Django's
        wrapper checks `has_permission` first (a login without it still goes
        to the admin's login page) and CSRF; this one runs inside it, so
        only a superuser is asked to confirm. The logout is let through: a
        session ending needs no password."""
        if view == self.logout:
            return super().admin_view(view, cacheable)
        return super().admin_view(_behind_the_confirmation(view), cacheable)

    @property
    def login_form(self):
        """/admin/login/ on the login page's counters. Imported here, not at
        the top: this module is imported while the apps load (INSTALLED_APPS
        names its config), before the User model the auth forms import can
        be."""
        from .forms import LimitedAdminAuthenticationForm

        return LimitedAdminAuthenticationForm

    @method_decorator(never_cache)
    @login_not_required
    def login(self, request, extra_context=None):
        """Django's, its answer handed to `limiter.remember_device`: the
        form's `limiter.succeeded` marks the request of a login that
        succeeded, and only that one gets the cookie (review of the
        LIMITER-LOCKOUT fix: this door honoured the cookie and never issued
        it) and the password confirmation. Public and never cached, as
        Django's own is."""
        from . import limiter, sudo

        response = limiter.remember_device(request, super().login(request, extra_context))
        if request.method == "POST" and limiter.logged_in_now(request) and request.user.is_authenticated:
            # The password was just checked: not asked again at the next page.
            # Not on any POST of a session already logged in: Django re-renders
            # a refused form with request.user still that session's login.
            sudo.stamp(request)
        return response


def _behind_the_confirmation(view):
    """`view` run only with the password confirmed, the confirmation then
    kept alive; otherwise the confirmation page, back to this admin page.
    Wrapped with the view's own attributes (`csrf_exempt` among them, which
    Django's wrapper reads)."""

    @functools.wraps(view)
    def confirmed_view(request, *args, **kwargs):
        # Imported here: this module loads with the apps (see login_form).
        from . import sudo

        if not sudo.confirmed(request):
            return sudo.ask(request)
        sudo.refresh(request)
        return view(request, *args, **kwargs)

    return confirmed_view


class MarginMateAdminConfig(AdminConfig):
    default_site = "accounts.admin_site.MarginMateAdminSite"
