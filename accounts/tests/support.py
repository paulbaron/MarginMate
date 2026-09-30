"""Tests of the espaces themselves: real espaces in temporary files.

The suite runs on ONE test espace, whose database is the test
runner's `default` and which every thread works for by default
(tests/runner.py). A test of the espaces needs what production starts from
instead:

* NOTHING bound by default: the suite's default binding is taken away for
  the class, so an unbound thread, request or command works for nobody, as
  in production - and the test espace's rows (its Tenant, its owner's login)
  are deleted at the start of each test, so the accounts database holds
  only what the test makes;
* an anonymous client (Django's own): log in with `force_login`;
* a TENANTS_ROOT per test (a temporary folder), with the template database
  already in it (copied from one migrated once per process), so
  `make_tenant` is the real `accounts.provisioning.create_tenant` and takes
  a few hundredths of a second;
* a TransactionTestCase with ``serialized_rollback = True`` (CLAUDE.md):
  binding an espace inside an open transaction on `default` is refused - its
  queries would leave it. The runner's snapshot puts the test espace back
  for the classes that follow.

The central rows are in the runner's own `accounts` test database, as the
router sends them in production.

Usage::

    from accounts.tests.support import TwoTenantsTestCase

    class MyPageTests(TwoTenantsTestCase):
        def test_bar_a_sees_only_its_rows(self):
            with bound_tenant(self.bar_a):
                make_supplier(name="Grossiste Alpha")
            with bound_tenant(self.bar_b):
                make_supplier(name="Grossiste Beta")
            self.client.force_login(self.user_a)
            page = self.client.get(reverse("invoices:supplier_list")).content.decode()
            self.assertIn("Grossiste Alpha", page)
            self.assertNotIn("Grossiste Beta", page)

`self.client` requests run through the real middleware: logged in, a
request is bound to the user's espace; `bound_tenant(...)` in the test body
reads or writes an espace directly. Never leave a binding open across a
`self.client` call - the middleware refuses to bind another espace inside
it.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from contextvars import ContextVar
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

from accounts import paths, provisioning, tenancy
from accounts.models import Membership, Tenant

#: Per process (the parallel runner's workers each build their own).
_MASTERS: dict = {}


def _masters_dir() -> Path:
    if "dir" not in _MASTERS:
        folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-espaces-masters-"))
        atexit.register(shutil.rmtree, folder, True)
        _MASTERS["dir"] = folder
    return _MASTERS["dir"]


def template_master() -> Path:
    """The espaces' template database, migrated once per process: the
    business apps' tables only, as `migrate_tenants` makes it."""
    root = _masters_dir() / "root"
    with override_settings(TENANTS_ROOT=root):
        path = paths.template_database()
        if not path.exists():
            provisioning.migrate_template()
    return path


class TenancyTestCase(TransactionTestCase):
    """Real espaces: a TENANTS_ROOT with its template, and `make_tenant` /
    `make_member` to fill them. Nothing bound unless the test binds it."""

    serialized_rollback = True
    #: Anonymous: a test here logs in as the member it made.
    client_class = Client

    @classmethod
    def setUpClass(cls):
        cls._espaces_tmp = Path(tempfile.mkdtemp(prefix="marginmate-tests-espaces-"))
        cls._suite_binding = tenancy._current
        tenancy._current = ContextVar("marginmate_current_tenant", default=None)
        try:
            template_master()
            super().setUpClass()
        except BaseException:
            tenancy._current = cls._suite_binding
            shutil.rmtree(cls._espaces_tmp, ignore_errors=True)
            raise
        cls.addClassCleanup(cls._remove_espaces)

    @classmethod
    def _remove_espaces(cls):
        """The class cleanup - unittest runs it right after tearDownClass, and
        when setUpClass failed after registering it: the suite's binding
        comes back, and the class's folders go."""
        tenancy._current = cls._suite_binding
        shutil.rmtree(cls._espaces_tmp, ignore_errors=True)

    def setUp(self):
        super().setUp()
        from tests.runner import TEST_EMAIL, TEST_TENANT_PK

        # The accounts database as production starts it: no test espace.
        Tenant.objects.filter(pk=TEST_TENANT_PK).delete()
        get_user_model().objects.filter(username=TEST_EMAIL).delete()
        self.tenants_root = Path(tempfile.mkdtemp(dir=self._espaces_tmp)) / "tenants"
        self.enterContext(override_settings(TENANTS_ROOT=self.tenants_root))
        template = paths.template_database()
        template.parent.mkdir(parents=True)
        provisioning.copy_database(template_master(), template)

    def make_tenant(self, name="Bar Essai", *, owner=False) -> Tenant:
        """A real espace (accounts.provisioning.create_tenant). `owner`: the
        one whose .env integrations work."""
        return provisioning.create_tenant(name, uses_server_integrations=owner)

    def make_member(self, tenant, email=None, *, role=Membership.Role.OWNER):
        """A login of `tenant`. Invented address; the password is
        irrelevant (tests log in with force_login)."""
        email = email or f"gerant-{tenant.dir_name}@example.invalid"
        user = get_user_model().objects.create_user(username=email, email=email, password="mot-de-passe-essai")
        Membership.objects.create(user=user, tenant=tenant, role=role)
        return user


class TwoTenantsTestCase(TenancyTestCase):
    """Two espaces with one login each: `bar_a`/`user_a` (the owner's espace
    when `owner_a` is set) and `bar_b`/`user_b`."""

    owner_a = False

    def setUp(self):
        super().setUp()
        self.bar_a = self.make_tenant("Bar Alpha", owner=self.owner_a)
        self.bar_b = self.make_tenant("Bar Beta")
        self.user_a = self.make_member(self.bar_a, "alpha@example.invalid")
        self.user_b = self.make_member(self.bar_b, "beta@example.invalid")
