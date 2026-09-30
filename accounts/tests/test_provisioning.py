"""Creating an espace, and migrating and commanding the espaces
(accounts/provisioning.py, manage.py migrate_tenants, manage.py tenant)."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from accounts import paths, provisioning
from accounts.models import Tenant
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import InvoiceType, Supplier

LEAF = ("invoices", "0035_supplier_typed_identifiers")


def pending_migrations() -> list:
    """What `migrate` would still apply to the bound `default`."""
    executor = MigrationExecutor(connections["default"])
    return executor.migration_plan(executor.loader.graph.leaf_nodes())


def applied(key) -> bool:
    return key in MigrationRecorder(connections["default"]).applied_migrations()


def columns(table) -> set:
    connection = connections["default"]
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


class CreateTenantTests(TenancyTestCase):
    def test_a_new_espace_is_a_migrated_copy_in_a_folder_of_its_own(self):
        tenant = provisioning.create_tenant("Le Comptoir d'Essai")
        self.assertTrue(Tenant.objects.filter(pk=tenant.pk, name="Le Comptoir d'Essai").exists())
        self.assertRegex(tenant.dir_name, r"^[a-z0-9]{12}$")
        self.assertNotIn("comptoir", tenant.dir_name)
        self.assertFalse(tenant.uses_server_integrations)
        folder = paths.tenant_dir(tenant)
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), sorted(["db.sqlite3", *paths.FOLDERS])
        )
        with bound_tenant(tenant):
            self.assertEqual(pending_migrations(), [])
            # What the seed migrations put in every database is there.
            self.assertTrue(Supplier.objects.filter(code="METRO").exists())

    def test_two_espaces_never_share_a_folder(self):
        one, other = provisioning.create_tenant("Bar Un"), provisioning.create_tenant("Bar Deux")
        self.assertNotEqual(one.dir_name, other.dir_name)

    def test_the_owner_s_integrations_are_switched_off_in_a_new_espace(self):
        tenant = provisioning.create_tenant("Bar Nouveau")
        with bound_tenant(tenant):
            self.assertFalse(Supplier.objects.get(code="METRO").is_scrapable)
            mailbox = InvoiceType.objects.filter(source_kind=InvoiceType.SourceKind.EMAIL)
            self.assertTrue(mailbox.exists())
            self.assertFalse(mailbox.filter(is_active=True).exists())

    def test_the_owner_s_espace_keeps_them(self):
        tenant = provisioning.create_tenant("Bar du Propriétaire", uses_server_integrations=True)
        with bound_tenant(tenant):
            self.assertTrue(Supplier.objects.get(code="METRO").is_scrapable)
            self.assertTrue(InvoiceType.objects.filter(source_kind="EMAIL", is_active=True).exists())

    def test_a_failure_leaves_nothing_behind(self):
        for step in ("switch_off_server_integrations", "_migrate_bound", "copy_database"):
            with self.subTest(step=step):
                with mock.patch.object(provisioning, step, side_effect=RuntimeError("panne d'essai")):
                    with self.assertRaisesMessage(RuntimeError, "panne d'essai"):
                        provisioning.create_tenant("Bar Raté")
                self.assertFalse(Tenant.objects.exists())
                self.assertEqual([p.name for p in paths.tenants_root().iterdir()], [paths.TEMPLATE_DIR])

    def test_a_taken_folder_is_never_removed(self):
        taken = paths.tenants_root() / "dossierpris1"
        taken.mkdir()
        (taken / "db.sqlite3").write_bytes(b"les donnees de quelqu'un")
        with self.assertRaises(FileExistsError):
            provisioning.create_tenant("Bar Intrus", dir_name="dossierpris1")
        self.assertEqual((taken / "db.sqlite3").read_bytes(), b"les donnees de quelqu'un")
        self.assertFalse(Tenant.objects.exists())

    def test_the_template_is_built_when_missing(self):
        paths.template_database().unlink()
        tenant = provisioning.create_tenant("Bar Premier")
        self.assertTrue(paths.template_database().is_file())
        with bound_tenant(tenant):
            self.assertEqual(pending_migrations(), [])
        # The template has no login tables: they live in the accounts database.
        with bound_tenant(tenant):
            self.assertNotIn("auth_user", connections["default"].introspection.table_names())

    def test_a_name_is_required(self):
        with self.assertRaises(ValueError):
            provisioning.create_tenant("   ")


class MigrateTenantsTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_tenant("Bar Alpha")
        self.beta = self.make_tenant("Bar Beta")
        for tenant in (self.alpha, self.beta):
            with bound_tenant(tenant):
                call_command("migrate", LEAF[0], "0034", verbosity=0, skip_checks=True)
                self.assertFalse(applied(LEAF))

    def test_every_espace_is_migrated_bound_to_itself(self):
        out = StringIO()
        call_command("migrate_tenants", stdout=out)
        for tenant in (self.alpha, self.beta):
            with bound_tenant(tenant):
                self.assertTrue(applied(LEAF), tenant.name)
                self.assertEqual(pending_migrations(), [])
                # Bound as `default`: its migrations wrote into its own file.
                self.assertIn("typed_identifiers", columns("invoices_supplier"))
        self.assertIn("Bar Alpha", out.getvalue())
        self.assertIn("Bar Beta", out.getvalue())

    def test_one_espace_only(self):
        call_command("migrate_tenants", "--tenant", self.alpha.dir_name, stdout=StringIO())
        with bound_tenant(self.alpha):
            self.assertTrue(applied(LEAF))
        with bound_tenant(self.beta):
            self.assertFalse(applied(LEAF))

    def test_one_espace_failing_does_not_stop_the_others(self):
        paths.tenant_database(self.alpha).unlink()
        err = StringIO()
        with self.assertRaisesMessage(CommandError, "Bar Alpha"):
            call_command("migrate_tenants", stdout=StringIO(), stderr=err)
        with bound_tenant(self.beta):
            self.assertTrue(applied(LEAF))
        self.assertIn("Bar Alpha", err.getvalue())

    def test_an_unknown_espace(self):
        with self.assertRaises(CommandError):
            call_command("migrate_tenants", "--tenant", "inconnu999", stdout=StringIO())

    def close(self, tenant):
        Tenant.objects.filter(pk=tenant.pk).update(is_active=False)

    def test_a_closed_espace_without_its_database_is_passed_over(self):
        """serve and DEPLOY.md §11 say « fermez l'espace » for a base that is
        gone, and serve then starts: migrate_tenants failed on it all the
        same, so deploy.cmd - which runs it after the merge - ended every
        deployment with the site down."""
        self.close(self.alpha)
        paths.tenant_database(self.alpha).unlink()
        out, err = StringIO(), StringIO()
        call_command("migrate_tenants", stdout=out, stderr=err)
        self.assertIn(f"Bar Alpha ({self.alpha.dir_name}) : fermé, sans base - ignoré.", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        with bound_tenant(self.beta):
            self.assertTrue(applied(LEAF))

    def test_a_closed_espace_that_does_not_migrate_is_a_warning(self):
        self.close(self.alpha)
        real = provisioning.migrate_tenant

        def failing_for_alpha(tenant, **kwargs):
            if tenant.pk == self.alpha.pk:
                raise RuntimeError("base abîmée")
            return real(tenant, **kwargs)

        out, err = StringIO(), StringIO()
        with mock.patch.object(provisioning, "migrate_tenant", side_effect=failing_for_alpha):
            call_command("migrate_tenants", stdout=out, stderr=err)
        self.assertIn(f"Bar Alpha ({self.alpha.dir_name}) : fermé, non migré (base abîmée)", err.getvalue())
        self.assertIn("avertissement", err.getvalue())
        with bound_tenant(self.beta):
            self.assertTrue(applied(LEAF))

    def test_an_open_espace_that_does_not_migrate_still_fails(self):
        real = provisioning.migrate_tenant

        def failing_for_alpha(tenant, **kwargs):
            if tenant.pk == self.alpha.pk:
                raise RuntimeError("base abîmée")
            return real(tenant, **kwargs)

        with (
            mock.patch.object(provisioning, "migrate_tenant", side_effect=failing_for_alpha),
            self.assertRaisesMessage(CommandError, "1 espace(s) non migré(s) : Bar Alpha"),
        ):
            call_command("migrate_tenants", stdout=StringIO(), stderr=StringIO())


class TenantCommandTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_tenant("Bar Alpha")
        self.beta = self.make_tenant("Bar Beta")
        with bound_tenant(self.alpha):
            Supplier.objects.create(code="T-ALPHA", name="Grossiste Alpha")
        with bound_tenant(self.beta):
            Supplier.objects.create(code="T-BETA", name="Grossiste Beta")

    def test_a_command_runs_bound_to_one_espace(self):
        out = StringIO()
        call_command("tenant", self.alpha.dir_name, "dumpdata", "invoices.Supplier", "--indent", "2", stdout=out)
        self.assertIn("Grossiste Alpha", out.getvalue())
        self.assertNotIn("Grossiste Beta", out.getvalue())
        # Its arguments reached it (--indent 2).
        self.assertIn('{\n  "model": "invoices.supplier"', out.getvalue())

    def test_refused_commands(self):
        for name in ("createsuperuser", "flush", "migrate_tenants", "tenant"):
            with self.subTest(name=name), self.assertRaises(CommandError):
                call_command("tenant", self.alpha.dir_name, name, stdout=StringIO())

    def test_an_unknown_espace(self):
        with self.assertRaises(CommandError):
            call_command("tenant", "inconnu999", "dumpdata", "invoices.Supplier", stdout=StringIO())
