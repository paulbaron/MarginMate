"""The test runner's contract (tests/runner.py): the whole suite runs as
production does - one database per tenant, a login on every page - on one
« test tenant » whose database is the runner's own `default`.

What every other test file takes for granted, pinned here:

* the main thread, a thread started WITHOUT `bound()` and a request all
  work for the test tenant, and a request's binding nests into it - it
  never swaps `default`, so a TestCase's transaction holds the rows the
  request reads and writes;
* every test client is logged in as the test tenant's owner from its first
  request, unless the test logs in, forces a login or logs out itself - his
  MarginMate password confirmed by that implicit login only;
* the central rows (logins, sessions, the tenant itself) live in the
  runner's own `accounts` test database, never in `default`;
* `TenancyTestCase` (accounts/tests/support.py) takes all of that away for
  its classes - nothing bound, no test tenant - so real tenants, threads
  started without `bound()`, signups and adoptions are still tested
  against what production starts from.

The runner replaces `accounts.tenancy._current`, a private name: if the
module renames it, the replacement binds nothing, and the first test here
says so before the thousands that would fail around it.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import connections, router
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from accounts import paths, sudo, tenancy
from accounts.models import Membership, Tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import Supplier
from tests import runner
from tests.factories import make_supplier


def in_a_raw_thread(function):
    """What `function` returns when run in a thread started WITHOUT
    `bound()` - a fresh thread-local and an empty contextvars context."""
    seen, errors = [], []

    def target():
        try:
            seen.append(function())
        except BaseException as exc:  # noqa: BLE001 - reported by the calling thread
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(30)
    if errors:
        raise errors[0]
    return seen[0]


class TheBindingTests(SimpleTestCase):
    """No query: the binding's default is a tenant built in memory."""

    def test_the_runner_replaces_the_binding_the_module_reads(self):
        self.assertIsInstance(tenancy._current, ContextVar)
        self.assertIs(tenancy._current.get(), runner.TEST_TENANT)

    def test_there_is_no_other_mode_to_run_in(self):
        """Single mode is gone (accounts/tests/test_no_single_mode.py): no
        switch in the settings, none in the binding's module."""
        from django.conf import settings

        self.assertFalse(hasattr(settings, "TENANCY_MODE"))
        self.assertFalse(hasattr(tenancy, "multi_mode"))

    def test_the_main_thread_works_for_the_test_tenant(self):
        tenant = tenancy.current_tenant()
        self.assertEqual(tenant.pk, runner.TEST_TENANT_PK)
        self.assertEqual(tenant.dir_name, runner.TEST_TENANT_DIR)
        self.assertEqual(tenancy.require_tenant().pk, runner.TEST_TENANT_PK)
        self.assertEqual(tenancy.tenant_key(), str(runner.TEST_TENANT_PK))
        # The owner's tenant: the suite has always assumed the server's
        # integrations were allowed (their credentials are blank anyway).
        self.assertTrue(tenancy.integrations_allowed())
        self.assertTrue(tenancy.server_accounts_allowed())

    def test_a_thread_started_without_bound_works_for_it_too(self):
        self.assertEqual(in_a_raw_thread(lambda: tenancy.current_tenant().pk), runner.TEST_TENANT_PK)

    def test_its_folders_are_under_the_test_tenants_root(self):
        folder = paths.media_root()
        self.assertEqual(folder, paths.tenant_dir(runner.TEST_TENANT) / paths.MEDIA)
        self.assertTrue(folder.is_dir())

    def test_every_database_test_uses_both_databases(self):
        self.assertEqual(TransactionTestCase.databases, {"default", "accounts"})
        self.assertEqual(TestCase.databases, {"default", "accounts"})

    def test_install_can_run_again(self):
        """The main process and each parallel worker run it; a second call
        changes nothing a test can see."""
        before = tenancy._current
        runner.install()
        self.addCleanup(setattr, tenancy, "_current", before)
        self.assertIs(tenancy._current.get(), runner.TEST_TENANT)
        self.assertIs(SimpleTestCase.client_class, runner.TenantClient)

    def test_the_parallel_workers_are_set_up_the_same_way(self):
        """Each worker of `--parallel` is a new process (spawned, on
        Windows): it runs `install()` before Django is set up in it."""
        self.assertIs(runner.TenantTestRunner.parallel_test_suite.process_setup, runner._worker_setup)


class TheTestTenantTests(TestCase):
    def test_its_rows_are_in_the_accounts_database(self):
        tenant = Tenant.objects.get(pk=runner.TEST_TENANT_PK)
        self.assertEqual(tenant._state.db, "accounts")
        self.assertEqual(tenant.dir_name, runner.TEST_TENANT_DIR)
        self.assertTrue(tenant.uses_server_integrations)
        user = runner.test_user()
        self.assertEqual(user._state.db, "accounts")
        self.assertEqual(Membership.objects.get(user=user).tenant_id, runner.TEST_TENANT_PK)

    def test_the_two_sides_are_two_databases(self):
        self.assertEqual(router.db_for_write(get_user_model()), "accounts")
        self.assertEqual(router.db_for_write(Supplier), "default")
        default_tables = set(connections["default"].introspection.table_names())
        accounts_tables = set(connections["accounts"].introspection.table_names())
        self.assertIn("invoices_supplier", default_tables)
        self.assertNotIn("auth_user", default_tables)
        self.assertNotIn("django_session", default_tables)
        self.assertIn("auth_user", accounts_tables)
        self.assertNotIn("invoices_supplier", accounts_tables)


class TheClientTests(TestCase):
    def test_it_is_logged_in_as_the_test_tenant_s_owner(self):
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
        self.assertEqual(response.wsgi_request.tenant.pk, runner.TEST_TENANT_PK)
        self.assertEqual(Session.objects.count(), 1)

    def test_a_request_nests_into_the_binding_without_swapping_default(self):
        """The row written in this test's transaction is on the page the
        request draws, and no tenant database was opened for it."""
        make_supplier(code="T-TEMOIN", name="Grossiste Témoin")
        before = connections["default"]
        with mock.patch("accounts.tenancy._wrapper_for", side_effect=AssertionError("default was swapped")):
            response = self.client.get(reverse("invoices:supplier_list"))
        self.assertContains(response, "Grossiste Témoin")
        self.assertIs(connections["default"], before)
        self.assertIs(tenancy._current.get(), runner.TEST_TENANT)

    def test_a_client_built_by_hand_is_logged_in_too(self):
        client = self.client_class(enforce_csrf_checks=True)
        self.assertEqual(client.get(reverse("invoices:supplier_list")).status_code, 200)

    def test_a_test_that_logs_in_is_not_logged_in_again(self):
        other = runner.member_of_the_test_tenant(
            get_user_model().objects.create_user(username="serveur@example.invalid", email="serveur@example.invalid")
        )
        self.client.force_login(other)
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, "serveur@example.invalid")

    def test_a_test_that_logs_out_is_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login")), response["Location"])

    def test_django_s_own_client_is_anonymous(self):
        """For the pages nobody logs in to (the employee's signing link)."""
        response = Client().get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 302)


class TheClientsConfirmationTests(TestCase):
    """The implicit login also confirms the owner's MarginMate password
    (accounts/sudo.py), so the suite posts to the protected pages - and only
    that login: a test logging in itself gets what production gives a login,
    no confirmation."""

    def stamp(self, client):
        return client.session.get(sudo.SESSION_KEY)

    def assertConfirmedFor(self, client, user):
        stamp = self.stamp(client)
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp["user"], user.pk)
        self.assertGreater(stamp["until"], time.time() + sudo.WINDOW_SECONDS - 60)

    def test_the_implicit_login_confirms_the_owner_s_password(self):
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
        self.assertConfirmedFor(self.client, runner.test_user())
        self.assertTrue(sudo.confirmed(response.wsgi_request))

    def test_a_client_built_by_hand_confirms_too(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.get(reverse("invoices:supplier_list"))
        self.assertConfirmedFor(client, runner.test_user())

    def test_a_login_the_test_makes_itself_confirms_nothing(self):
        self.client.force_login(runner.test_user())
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
        self.assertIsNone(self.stamp(self.client))
        self.assertFalse(sudo.confirmed(response.wsgi_request))

    def test_a_client_may_log_in_unconfirmed(self):
        for client in (self.client_class(confirms_password=False), self.client_class()):
            with self.subTest(client=client):
                client.confirms_password = False
                response = client.get(reverse("invoices:supplier_list"))
                self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
                self.assertIsNone(self.stamp(client))

    def test_confirm_password_logs_in_first_and_writes_the_stamp(self):
        other = runner.member_of_the_test_tenant(
            get_user_model().objects.create_user(username="serveur@example.invalid", email="serveur@example.invalid")
        )
        self.assertEqual(runner.confirm_password(self.client, other), other)
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, "serveur@example.invalid")
        self.assertConfirmedFor(self.client, other)
        # The owner by default, and an ended one on demand.
        runner.confirm_password(self.client, seconds=-1)
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
        self.assertFalse(sudo.confirmed(response.wsgi_request))

    def test_forget_the_confirmation_takes_it_back(self):
        self.client.get(reverse("invoices:supplier_list"))
        runner.forget_the_confirmation(self.client)
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.wsgi_request.user.username, runner.TEST_EMAIL)
        self.assertIsNone(self.stamp(self.client))


class NothingBoundInTenancyTestCaseTests(TenancyTestCase):
    """A TenancyTestCase starts from what production starts from."""

    def test_nothing_is_bound_and_there_is_no_test_tenant(self):
        self.assertIsNone(tenancy.current_tenant())
        self.assertIsNone(in_a_raw_thread(tenancy.current_tenant))
        self.assertFalse(Tenant.objects.filter(pk=runner.TEST_TENANT_PK).exists())
        self.assertFalse(get_user_model().objects.filter(username=runner.TEST_EMAIL).exists())

    def test_its_client_is_anonymous(self):
        self.assertIs(self.client_class, Client)
        self.assertEqual(self.client.get(reverse("invoices:supplier_list")).status_code, 302)
