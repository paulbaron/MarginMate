"""The test runner: the whole suite runs as production does - one database
per tenant, a login on every page, the only mode there is
(config/settings_test.py: ``TEST_RUNNER = "tests.runner.TenantTestRunner"``).

**The test tenant.** One tenant - `Tenant` pk 1, the owner's (its server
integrations allowed, as the suite always assumed; their credentials are
blank in the test settings) - with one owner login and its membership. They
are written into the `accounts` test database WHILE it is migrated
(post_migrate, `_create_test_tenant`), which is before the parallel
runner clones it and before the snapshot every TransactionTestCase restores
is taken: every worker and every restore has them.

**Its database IS the runner's `default`.** In the test process the
binding's DEFAULT is the test tenant (`accounts.tenancy._current` is
replaced by a ContextVar whose default is `TEST_TENANT`). So the main
thread, every thread a test starts - with `bound()` or without - and every
request work for it without `default` ever being swapped: the middleware
finds the logged-in owner's tenant, the same one, and `bound_tenant` nests
into it as a no-op. A TestCase's transaction therefore holds everything a
request reads and writes, as it always did. Its folders are the real
`accounts.paths` ones, under a temporary TENANTS_ROOT.

**Every test client is logged in** as the test tenant's owner from its
first request (`TenantClient`), unless the test logs in, forces a login or
logs out itself - so every page a test fetches goes through the real
LoginRequiredMiddleware, TenantMiddleware, file storage and integrations
gate. Django's own `Client()` stays anonymous: the employee's signing pages
are fetched by nobody logged in. A browser test logs its Chrome in with
`log_in_the_browser`.

**That implicit login also confirms his MarginMate password**
(accounts/sudo.py, as the confirmation page records it), so a test posts to
« Identifiants », a portal's source, « Données »'s import and clear and the
admin as freely as it reads a page. Only that one: a test that logs in
itself (`force_login`, `login`) gets the session production gives a login -
none - and writes one with `confirm_password` where it wants it; a client
built with `confirms_password=False` logs in unconfirmed, and
`forget_the_confirmation` takes one back - for a test about the gate.

**`TenancyTestCase`** (accounts/tests/support.py) takes all of that away for
its classes: nothing bound by default, no test tenant rows, an anonymous
client - production's starting point, for real tenants in real files.

What this sets for the whole run, in the main process and in every parallel
worker (`install`): `TransactionTestCase.databases` (both test databases),
`SimpleTestCase.client_class` (the logged-in client), the binding's
default, and a push transport that raises (`notifications.webpush._post`,
replaced on the module: a test wanting an answer patches it or passes
`post=`). Nothing in the product knows about any of it: no hook, no
setting read only by tests. tests/test_runner.py pins the contract.
"""

from __future__ import annotations

import time
import traceback
from contextvars import ContextVar

from django.conf import settings
from django.db.models.signals import post_migrate
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.test.runner import DiscoverRunner, ParallelTestSuite, RemoteTestResult, RemoteTestRunner
from django.utils.functional import SimpleLazyObject

TEST_TENANT_PK = 1
#: Its folder under the test settings' TENANTS_ROOT (a temporary folder).
TEST_TENANT_DIR = "espace-des-tests"
TEST_TENANT_NAME = "Bar des tests"
#: The owner's login. Invented, like every address of the suite.
TEST_EMAIL = "gerant-tests@example.invalid"

_TENANT_SIGNAL = "tests.runner.test_tenant"


def _build_test_tenant():
    from accounts.models import Tenant

    tenant = Tenant(
        pk=TEST_TENANT_PK,
        name=TEST_TENANT_NAME,
        dir_name=TEST_TENANT_DIR,
        uses_server_integrations=True,
        is_active=True,
    )
    # As if read from the accounts database, where its row is.
    tenant._state.adding = False
    tenant._state.db = "accounts"
    return tenant


#: The test tenant, built in memory on first use (no query: a SimpleTestCase
#: and a thread read it as freely as a TestCase).
TEST_TENANT = SimpleLazyObject(_build_test_tenant)


def test_user():
    """The test tenant's owner (a query on the accounts database)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username=TEST_EMAIL)


def member_of_the_test_tenant(user):
    """`user`, given a membership of the test tenant: a login with none meets
    the « aucun espace » page. Returns `user`."""
    from accounts.models import Membership

    Membership.objects.get_or_create(user=user, tenant_id=TEST_TENANT_PK)
    return user


def employee_of_the_test_tenant(email, pages=(), *, name="", password=None):
    """A new login, an EMPLOYEE (a MEMBER) of the test tenant opening the
    areas `pages` (accounts/access.py) - log a client in as him with
    `force_login`: a TenantClient's implicit login is the owner's. With no
    `password`, an unusable one. Returns the login."""
    from django.contrib.auth import get_user_model

    from accounts.models import Membership

    user = get_user_model().objects.create_user(username=email, email=email, first_name=name, password=password)
    Membership.objects.create(user=user, tenant_id=TEST_TENANT_PK, role=Membership.Role.MEMBER, pages=list(pages))
    return user


def confirm_password(client, user=None, seconds=None):
    """`user`'s MarginMate password confirmed in `client`'s session, as
    accounts/sudo.py records it once typed (a time and the login's pk) -
    `user` the test tenant's owner unless given, logged in first when the
    session is not his: a session made before a TenantClient's first request
    would otherwise leave it anonymous. For `seconds` from now
    (`sudo.WINDOW_SECONDS` by default; negative: a confirmation that has
    ended). Returns `user`."""
    from django.contrib.auth import SESSION_KEY as LOGIN_SESSION_KEY

    user = user or test_user()
    if client.session.get(LOGIN_SESSION_KEY) != str(user.pk):
        client.force_login(user)
    _write_confirmation(client, user, seconds)
    return user


def forget_the_confirmation(client) -> None:
    """No confirmation left in `client`'s session: the next protected
    request asks for the password again."""
    from accounts import sudo

    session = client.session
    session.pop(sudo.SESSION_KEY, None)
    session.save()


def _write_confirmation(client, user, seconds=None) -> None:
    from accounts import sudo

    window = sudo.WINDOW_SECONDS if seconds is None else seconds
    session = client.session
    session[sudo.SESSION_KEY] = {"user": user.pk, "until": time.time() + window}
    session.save()


class TenantClient(Client):
    """Logged in as the test tenant's owner from its first request, his
    MarginMate password confirmed (`confirms_password`, True unless the
    client is built with False) - unless the test logs in, forces a login or
    logs out first, or the client already carries a session. A login the test
    makes itself confirms nothing (`confirm_password` does)."""

    def __init__(self, *args, confirms_password=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_in_first = True
        #: Whether the implicit first login also confirms the password;
        #: may be set on the client until its first request.
        self.confirms_password = confirms_password

    def request(self, **request):
        if self._log_in_first:
            self._log_in_first = False
            if settings.SESSION_COOKIE_NAME not in self.cookies:
                owner = test_user()
                # Django's own login, not this class's: the implicit one alone
                # confirms.
                super().force_login(owner)
                if self.confirms_password:
                    _write_confirmation(self, owner)
        return super().request(**request)

    def login(self, **credentials):
        self._log_in_first = False
        return super().login(**credentials)

    def force_login(self, user, backend=None):
        self._log_in_first = False
        return super().force_login(user, backend)

    def logout(self):
        self._log_in_first = False
        return super().logout()


def log_in_the_browser(driver, live_server_url, user=None) -> None:
    """Log a browser test's Chrome in, as `user` or the test tenant's owner:
    a session made by a test client, its cookie handed to the browser. On a
    page of this site first - a cookie belongs to the page's own host - and
    the login page, which needs no login. Call it in `setUp`: a
    TransactionTestCase empties the sessions after every test."""
    from django.urls import reverse

    client = Client()
    client.force_login(user or test_user())
    driver.get(live_server_url + reverse("accounts:login"))
    driver.add_cookie(
        {
            "name": settings.SESSION_COOKIE_NAME,
            "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
            "path": "/",
        }
    )


def _create_test_tenant(sender, using, **kwargs):
    """post_migrate, while the runner builds the `accounts` test database."""
    from accounts.router import ACCOUNTS_ALIAS

    if using != ACCOUNTS_ALIAS or getattr(sender, "label", None) != "accounts":
        return
    from django.contrib.auth import get_user_model

    from accounts.models import Membership, Tenant

    Tenant.objects.using(using).update_or_create(
        pk=TEST_TENANT_PK,
        defaults={
            "name": TEST_TENANT_NAME,
            "dir_name": TEST_TENANT_DIR,
            "uses_server_integrations": True,
            "is_active": True,
        },
    )
    user, _ = (
        get_user_model().objects.db_manager(using).get_or_create(username=TEST_EMAIL, defaults={"email": TEST_EMAIL})
    )
    Membership.objects.using(using).get_or_create(
        user=user, tenant_id=TEST_TENANT_PK, defaults={"role": Membership.Role.OWNER}
    )


def install() -> None:
    """What every process of the run needs: the main one, and each parallel
    worker before Django is set up in it. Safe to call twice."""
    from accounts import tenancy
    from notifications import webpush
    from tests.support import _Forbidden, _ForbiddenPush

    TransactionTestCase.databases = frozenset({"default", "accounts"})
    SimpleTestCase.client_class = TenantClient
    tenancy._current = ContextVar("marginmate_current_tenant", default=TEST_TENANT)
    # No test reaches a real push service: the one HTTP call of the push
    # transport raises for the whole run, whatever the test case's class. A
    # test that wants an answer passes `post=` to webpush.send or patches
    # `notifications.webpush._post` itself. What it raises is no Exception
    # (`ForbiddenNetworkCall`), so the delivery's own `except Exception`
    # cannot turn a forgotten mock into a quiet « erreur interne ».
    if not isinstance(webpush._post, _Forbidden):
        webpush._post = _ForbiddenPush("push service (notifications.webpush._post)")


def _worker_setup(*args) -> None:
    install()


class _SubTestByName:
    """What the main process does with a failing subtest: print its name.
    The subtest itself carries its whole test case - a test client that has
    answered a request included, which does not pickle."""

    def __init__(self, subtest):
        self.name = str(subtest)
        self.test_id = subtest.id()
        self.doc = subtest.shortDescription()
        self.failureException = subtest.failureException

    def __str__(self):
        return self.name

    def id(self):
        return self.test_id

    def shortDescription(self):
        return self.doc


class _TracebackAsTextResult(RemoteTestResult):
    """A parallel worker's failure travels back as its formatted text.

    Django sends each failure's (type, value, traceback) from the worker to
    the main process by pickling it, which a traceback does not survive
    without tblib - not installed, and not worth a dependency: the first
    failing test killed the whole parallel run (« tracebacks cannot be
    pickled ») instead of being reported with the others. A failing subtest
    travels as its name (`_SubTestByName`), for the same reason."""

    @staticmethod
    def _as_text(err, kind):
        return (kind, kind("".join(traceback.format_exception(*err))), None)

    def addError(self, test, err):
        super().addError(test, self._as_text(err, RuntimeError))

    def addFailure(self, test, err):
        super().addFailure(test, self._as_text(err, AssertionError))

    def addSubTest(self, test, subtest, err):
        if err is not None:
            kind = AssertionError if issubclass(err[0], test.failureException) else RuntimeError
            err = self._as_text(err, kind)
            subtest = _SubTestByName(subtest)
        super().addSubTest(test, subtest, err)


class _TracebackAsTextRunner(RemoteTestRunner):
    resultclass = _TracebackAsTextResult


class TenantParallelTestSuite(ParallelTestSuite):
    process_setup = _worker_setup
    runner_class = _TracebackAsTextRunner


class TenantTestRunner(DiscoverRunner):
    parallel_test_suite = TenantParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install()

    def setup_databases(self, **kwargs):
        # Only while the test databases are built: a test that migrates the
        # accounts database itself (migrate_tenants) must not get the test
        # tenant back - it would then look for its file.
        post_migrate.connect(_create_test_tenant, dispatch_uid=_TENANT_SIGNAL)
        try:
            return super().setup_databases(**kwargs)
        finally:
            post_migrate.disconnect(dispatch_uid=_TENANT_SIGNAL)
