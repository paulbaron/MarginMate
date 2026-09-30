"""The admin site, as the platform's owner's tool.

Installed in place of django.contrib.admin's own config
(config/settings.py INSTALLED_APPS, Django's documented way of replacing the
default site): every app's `admin.site.register` lands on this site.

Superusers only. Staff alone is not enough: the admin lists every login of
every bar (auth.User) and edits raw rows, and a signup never sets is_staff
anyway. The business models it shows are the superuser's own espace's - the
request is bound to it like any page (accounts.middleware.TenantMiddleware).
Its login page, Django's and public, counts its attempts on the login
page's counters (accounts/limiter.py), and a login that succeeds there
leaves the « appareil connu » cookie as the login page's does. Its logout is
Django's own.
"""

from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache


class MarginMateAdminSite(admin.AdminSite):
    def has_permission(self, request):
        user = request.user
        return bool(user.is_active and user.is_superuser)

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
        it). Public and never cached, as Django's own is."""
        from . import limiter

        return limiter.remember_device(request, super().login(request, extra_context))


class MarginMateAdminConfig(AdminConfig):
    default_site = "accounts.admin_site.MarginMateAdminSite"
