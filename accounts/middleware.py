"""The two middlewares of the tenants (settings.MIDDLEWARE, right after
AuthenticationMiddleware).

`LoginRequiredMiddleware`: Django's own (deny by default; public views
carry ``@login_not_required``), always in effect - there is no mode without
a login any more (the single mode that had none was removed on 29/09/2026).

`TenantMiddleware`: a logged-in user's request runs INSIDE
``bound_tenant(<his tenant>)`` from start to finish - the view, the
TemplateResponse's rendering and the context processors (the navigation's
badges) included. An anonymous request runs unbound, and so does EVERY
request to a public view (``@login_not_required``), logged in or not: those
views are no login's business. The employee's signing pages bind the LINK's
tenant themselves, whoever is logged in on that browser (a manager of
another bar lending his phone, an owner of two bars); the login, the logout
and the signup need none. Bound to the visitor's own tenant, the link of
another could not be opened in the same request. A user with no tenant
gets a plain « aucun espace » page, never a traceback. Every answer bound
to a tenant is sent « never keep this » (no-store): the browser of a
shared device must not draw one bar's page again after a logout.

**A session belongs to the tenant it was opened in** (security audit
LOAD-1). The tenant is looked up again on every request, so a login moved to
another bar - a membership edited in the admin - opened the NEW bar's pages
in every browser still logged in as him, the old bar's shared PC included,
without anyone typing a password. Every login now writes its tenant into the
session (`pin_the_tenant`, on ``user_logged_in``: a login again in the same
session - Django keeps its data - is pinned again too), and a request whose
tenant is not the session's is logged out and sent to the login page (an
htmx request: a 401 whose HX-Redirect sends the whole page), with
« Votre accès a changé : reconnectez-vous. ». A login with no tenant when it
logged in logs in again once it has one. A session opened before pins
existed (29/09/2026) is pinned to where it is on its next request - and
told the storage prefix its pages used (`LEGACY_STORAGE_SESSION_KEY`, below).

**A tenant whose database will not open** - the file missing, a tenant
being restored, a file that is no database - answers « Votre espace est
momentanément indisponible », a 503 no-store page rendered unbound, the
cause in the server's log (audit LOAD-3): it was a bare 500 on every page, a
batch's status polled every second included. Only the binding and the
opening of its connection are caught, and the binding is undone: a view's
errors stay the view's. (An EMPTY file opens - SQLite takes it for a new
database - and fails in the view: « no such table », a 500.)

**The browser's storage keys carry an opaque scope** (``request.
storage_scope``, base.html's ``<body data-tenant>``,
`accounts.tenancy.storage_scope`), not the tenant's sequential id (audit
LB-6).

Sync on purpose: the binding is thread-local, and under ASGI a sync view
runs in another thread than an async middleware - deploy with WSGI.
"""

import logging
from contextlib import ExitStack
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, user_logged_in
from django.contrib.auth.middleware import LoginRequiredMiddleware as DjangoLoginRequiredMiddleware
from django.contrib.auth.views import redirect_to_login
from django.contrib.messages.storage import default_storage
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.dispatch import receiver
from django.http import FileResponse, HttpResponse
from django.shortcuts import render, resolve_url
from django.urls import Resolver404, resolve
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme

from .access import Access
from .tenancy import TenancyError, bound_tenant, storage_scope

logger = logging.getLogger(__name__)

#: The session's tenant: the pk of the tenant the login worked in when it
#: logged in (None: it had none).
TENANT_SESSION_KEY = "_marginmate_espace"
#: Set on a session opened before sessions were pinned: its pages were
#: shown the tenant's id and keyed the browser's storage with it, so they
#: are still given it (``<body data-tenant-legacy>``) for
#: static/js/tenant_storage_legacy.js to move those keys under the new
#: scope - nothing that session did not already see. A login never sets it.
LEGACY_STORAGE_SESSION_KEY = "_marginmate_storage_legacy"
#: Said on the login page a session moved to another tenant lands on.
ACCESS_CHANGED = "Votre accès a changé : reconnectez-vous."


def htmx_to_login(request, login_url, redirect_field_name="next"):
    """The answer to an htmx request that must go to the login page: a 401
    carrying ``HX-Redirect`` instead of a 302 - the XHR would follow the
    redirect silently and swap the login page into a job's card, or blank
    the tab. htmx sends the whole page to the login page, with the page the
    user was on (``HX-Current-URL``, this site only) as `next`."""
    page = request.headers.get("HX-Current-URL", "")
    if url_has_allowed_host_and_scheme(page, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        parts = urlsplit(page)
        back = parts.path + (f"?{parts.query}" if parts.query else "")
    else:
        back = "/"
    login = redirect_to_login(back or "/", login_url, redirect_field_name)
    answer = HttpResponse(status=401)
    answer["HX-Redirect"] = login["Location"]
    return answer


class LoginRequiredMiddleware(DjangoLoginRequiredMiddleware):
    """Django's LoginRequiredMiddleware, with one change: an htmx request (a
    job's status polled every second, a boosted tab) gets `htmx_to_login`'s
    401 instead of the 302."""

    def handle_no_permission(self, request, view_func):
        response = super().handle_no_permission(request, view_func)
        if request.headers.get("HX-Request") != "true":
            return response
        return htmx_to_login(
            request, resolve_url(self.get_login_url(view_func)), self.get_redirect_field_name(view_func)
        )


def membership_of(user):
    """The membership a logged-in user works through: his first of an
    active tenant (one per user today), its tenant with it - the role and
    the pages it opens (accounts/access.py) come in the same row. One query
    on the accounts database; None when he has none."""
    from .models import Membership

    return Membership.objects.select_related("tenant").filter(user=user, tenant__is_active=True).order_by("pk").first()


def tenant_of(user):
    """The tenant a logged-in user works in (`membership_of`'s), or None."""
    membership = membership_of(user)
    return membership.tenant if membership else None


@receiver(user_logged_in, dispatch_uid="accounts.middleware.pin_the_tenant")
def pin_the_tenant(sender, request, user, **kwargs):
    """Every login - the login page, the signup, the admin's, a test's
    force_login - writes the tenant it logs into into its session. A login
    again as the same user keeps the session's data (Django's cycle_key),
    so the pin is written again, never carried over."""
    session = getattr(request, "session", None)
    if session is None:
        return
    tenant = tenant_of(user)
    session[TENANT_SESSION_KEY] = tenant.pk if tenant else None
    session.pop(LEGACY_STORAGE_SESSION_KEY, None)


def _session_allows(request, tenant) -> bool:
    """Whether this request's session may work for `tenant`: the tenant its
    login logged into. A session from before the pins is pinned here, to
    where it is. A request with no session at all (built by hand) has
    nothing to tie."""
    session = getattr(request, "session", None)
    if session is None:
        return True
    if TENANT_SESSION_KEY not in session:
        session[TENANT_SESSION_KEY] = tenant.pk
        session[LEGACY_STORAGE_SESSION_KEY] = True
        return True
    return session[TENANT_SESSION_KEY] == tenant.pk


def _logged_out_for_another_tenant(request, tenant):
    """Log the session out and send it to the login page, which says why."""
    logger.warning(
        "Session fermée : le compte %s travaille maintenant dans l'espace %s, pas dans celui de sa connexion (%s).",
        request.user.pk,
        tenant.pk,
        request.session.get(TENANT_SESSION_KEY),
    )
    logout(request)
    login_url = resolve_url(settings.LOGIN_URL)
    if request.headers.get("HX-Request") == "true":
        response = htmx_to_login(request, login_url)
    else:
        response = redirect_to_login(request.get_full_path(), login_url)
    # MessageMiddleware comes after this one and never sees this request:
    # the message is stored here, in its cookie.
    storage = default_storage(request)
    storage.add(messages.INFO, ACCESS_CHANGED)
    storage.update(response)
    return response


def _scope_the_storage(request, tenant) -> None:
    """What base.html's <body data-tenant> (and data-tenant-legacy) is drawn
    from: the tenant's opaque scope - worked out from its pk and the key,
    no database read - and, for a session from before the pins, its old id."""
    request.storage_scope = storage_scope(tenant)
    session = getattr(request, "session", None)
    request.storage_legacy = tenant.pk if session is not None and session.get(LEGACY_STORAGE_SESSION_KEY) else None


def _unavailable(request, tenant):
    """« Votre espace est momentanément indisponible »: rendered unbound,
    never kept by the browser. Its « Se déconnecter » forgets the tenant's
    drafts as the topbar's does (ui.js, `form.topbar-logout`), so the page
    carries the tenant's scope."""
    _scope_the_storage(request, tenant)
    response = render(request, "accounts/unavailable.html", status=503)
    add_never_cache_headers(response)
    return response


def _is_public(request) -> bool:
    """Whether the view this request reaches is public
    (``@login_not_required``): it runs unbound, for a user with no tenant
    too - the logout, for one."""
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return False
    return not getattr(match.func, "login_required", True)


def _bound_chunks(tenant, chunks):
    """A streamed body read chunk by chunk inside the binding, which is
    released between chunks: the response is read after this middleware
    returns, and a binding left open on a server thread would be the next
    request's."""
    iterator = iter(chunks)
    try:
        while True:
            with bound_tenant(tenant):
                try:
                    chunk = next(iterator)
                except StopIteration:
                    return
            yield chunk
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or _is_public(request):
            # Nothing of the visitor's is bound (the module's docstring).
            return self.get_response(request)

        membership = membership_of(user)
        if membership is None:
            return render(request, "accounts/no_tenant.html", status=403)
        tenant = membership.tenant
        if not _session_allows(request, tenant):
            return _logged_out_for_another_tenant(request, tenant)

        # The binding is entered on its own, and the tenant's connection
        # opened in it (its PRAGMAs read the file), so that only THEIR
        # failure - the database missing, or no database - becomes the
        # « indisponible » page; the view below runs inside the binding,
        # outside this try.
        stack = ExitStack()
        try:
            stack.enter_context(bound_tenant(tenant))
            connections[DEFAULT_DB_ALIAS].ensure_connection()
        except (TenancyError, DatabaseError):
            stack.close()
            logger.exception("Espace %s indisponible : sa base ne s'ouvre pas, page 503 servie.", tenant.pk)
            return _unavailable(request, tenant)

        request.tenant = tenant
        # What this login may open (accounts/access.py, whose
        # AccessMiddleware is the gate): read from the same row, no query.
        request.membership = membership
        request.access = Access.of(membership)
        _scope_the_storage(request, tenant)
        with stack:
            response = self.get_response(request)
            # Rendered here, inside the binding: its context processors
            # and template tags read the tenant's database.
            if hasattr(response, "render") and callable(response.render) and not response.is_rendered:
                response = response.render()
        # A bar's page is never kept by the browser: on a shared device,
        # Back after a logout - or after another bar's login - drew it again
        # from the back/forward cache, the bar's rows and its name included
        # (accounts/tests/test_back_button_browser.py). Merged into what the
        # view said: the file view's « private, no-store » stays.
        add_never_cache_headers(response)
        if getattr(response, "streaming", False) and not (
            isinstance(response, FileResponse) and response.file_to_stream is not None
        ):
            # An already open file needs no database; anything else is
            # read inside the binding, one chunk at a time.
            response.streaming_content = _bound_chunks(tenant, response.streaming_content)
        return response
