"""Which database a model's rows live in (accounts/router.py)."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.db import router
from django.test import SimpleTestCase

from accounts.models import PushDevice, SigningLink, Tenant
from accounts.router import ACCOUNTS_APPS, AccountsRouter
from accounts.tests.support import TenancyTestCase
from invoices.models import Supplier
from notifications.models import Dispatch
from staff.models import SignatureRequest


class NoForeignKeyAcrossTests(SimpleTestCase):
    """The two sides are different SQLite files in multi mode: a foreign
    key between them could never be enforced, nor joined."""

    def test_no_business_model_points_at_a_central_one(self):
        crossing = []
        for model in apps.get_models(include_auto_created=True):
            central = model._meta.app_label in ACCOUNTS_APPS
            for field in model._meta.get_fields():
                target = getattr(field, "related_model", None)
                if not field.is_relation or target is None or not getattr(field, "concrete", False):
                    continue
                if (target._meta.app_label in ACCOUNTS_APPS) != central:
                    crossing.append(f"{model._meta.label}.{field.name} -> {target._meta.label}")
        self.assertEqual(crossing, [])

    def test_the_central_apps_are_the_ones_the_spec_names(self):
        self.assertEqual(ACCOUNTS_APPS, {"auth", "contenttypes", "sessions", "admin", "accounts"})


class MultiModeRoutingTests(TenancyTestCase):
    def test_central_rows_go_to_accounts_business_rows_to_the_bound_default(self):
        for model in (get_user_model(), Session, ContentType, Tenant, SigningLink, PushDevice):
            with self.subTest(model=model.__name__):
                self.assertEqual(router.db_for_read(model), "accounts")
                self.assertEqual(router.db_for_write(model), "accounts")
        for model in (Supplier, SignatureRequest, Dispatch):
            with self.subTest(model=model.__name__):
                self.assertEqual(router.db_for_read(model), "default")
                self.assertEqual(router.db_for_write(model), "default")

    def test_each_side_is_migrated_in_its_own_database_only(self):
        rules = AccountsRouter()
        for app in ACCOUNTS_APPS:
            with self.subTest(app=app):
                self.assertIs(rules.allow_migrate("accounts", app), True)
                self.assertIs(rules.allow_migrate("default", app), False)
        for app in (
            "invoices",
            "inventory",
            "recipes",
            "bank",
            "margins",
            "transfer",
            "staff",
            "returnables",
            "notifications",
        ):
            with self.subTest(app=app):
                self.assertIs(rules.allow_migrate("default", app), True)
                self.assertIs(rules.allow_migrate("accounts", app), False)

    def test_a_tenant_file_has_no_login_table_and_the_accounts_file_no_business_table(self):
        from django.db import connections

        from accounts.tenancy import bound_tenant

        tenant = self.make_tenant()
        with bound_tenant(tenant):
            tenant_tables = set(connections["default"].introspection.table_names())
        accounts_tables = set(connections["accounts"].introspection.table_names())
        self.assertIn("invoices_supplier", tenant_tables)
        self.assertNotIn("auth_user", tenant_tables)
        self.assertNotIn("django_session", tenant_tables)
        self.assertNotIn("accounts_tenant", tenant_tables)
        self.assertIn("auth_user", accounts_tables)
        self.assertIn("accounts_signinglink", accounts_tables)
        self.assertNotIn("invoices_supplier", accounts_tables)

    def test_a_row_stays_in_the_database_it_came_from(self):
        user = get_user_model().objects.create_user(username="lecteur@example.invalid")
        self.assertEqual(user._state.db, "accounts")
        self.assertEqual(router.db_for_write(get_user_model(), instance=user), "accounts")
