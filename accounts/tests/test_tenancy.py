"""The binding: which tenant a thread works for (accounts/tenancy.py).

For real - two tenants in two temporary files.
"""

import sqlite3
import threading

from django.conf import settings
from django.db import connections, transaction

from accounts import paths
from accounts.models import Tenant
from accounts.tenancy import (
    NoTenantBound,
    TenancyError,
    bound,
    bound_tenant,
    current_tenant,
    integrations_allowed,
    require_tenant,
    server_accounts_allowed,
    tenant_key,
)
from accounts.tests.support import TenancyTestCase
from invoices.models import Supplier


def names_on_disk(tenant, prefix="T-"):
    """What the tenant's own file holds, read without Django."""
    with sqlite3.connect(paths.tenant_database(tenant)) as raw:
        rows = raw.execute("SELECT name FROM invoices_supplier WHERE code LIKE ? ORDER BY name", (f"{prefix}%",))
        return [name for (name,) in rows]


class BindingTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_tenant("Bar Alpha")
        self.beta = self.make_tenant("Bar Beta")

    def test_a_binding_reads_and_writes_the_tenant_s_own_file(self):
        with bound_tenant(self.alpha):
            Supplier.objects.create(code="T-ALPHA", name="Grossiste Alpha")
            self.assertEqual(current_tenant(), self.alpha)
        with bound_tenant(self.beta):
            self.assertFalse(Supplier.objects.filter(code="T-ALPHA").exists())
        self.assertEqual(names_on_disk(self.alpha), ["Grossiste Alpha"])
        self.assertEqual(names_on_disk(self.beta), [])

    def test_two_threads_bound_to_two_tenants_at_once(self):
        """Both threads are bound AT THE SAME TIME (a barrier holds them),
        each writes and reads its own file, and the main thread's `default`
        and the shared settings dict never move."""
        barrier = threading.Barrier(2, timeout=30)
        seen, errors = {}, []
        main_default = connections["default"]
        shared_name = connections.settings["default"]["NAME"]

        def work(tenant, name):
            try:
                with bound_tenant(tenant):
                    Supplier.objects.create(code=f"T-{name.upper()}", name=f"Grossiste {name}")
                    barrier.wait()
                    seen[tenant.pk] = (
                        list(Supplier.objects.filter(code__startswith="T-").values_list("name", flat=True)),
                        connections["default"].settings_dict["NAME"],
                        current_tenant().pk,
                    )
                    barrier.wait()
            except BaseException as exc:  # noqa: BLE001 - reported by the main thread
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=work, args=(self.alpha, "Alpha")),
            threading.Thread(target=work, args=(self.beta, "Beta")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(seen[self.alpha.pk][0], ["Grossiste Alpha"])
        self.assertEqual(seen[self.beta.pk][0], ["Grossiste Beta"])
        self.assertEqual(seen[self.alpha.pk][1], str(paths.tenant_database(self.alpha)))
        self.assertEqual(seen[self.beta.pk][1], str(paths.tenant_database(self.beta)))
        self.assertEqual(seen[self.alpha.pk][2], self.alpha.pk)
        self.assertEqual(seen[self.beta.pk][2], self.beta.pk)
        self.assertEqual(names_on_disk(self.alpha), ["Grossiste Alpha"])
        self.assertEqual(names_on_disk(self.beta), ["Grossiste Beta"])
        self.assertIs(connections["default"], main_default)
        self.assertEqual(connections.settings["default"]["NAME"], shared_name)
        self.assertEqual(settings.DATABASES["default"]["NAME"], shared_name)
        self.assertIsNone(current_tenant())

    def test_the_shared_settings_dict_is_never_mutated(self):
        before = dict(connections.settings["default"])
        with bound_tenant(self.alpha):
            self.assertEqual(dict(connections.settings["default"]), before)
            self.assertIsNot(connections["default"].settings_dict, connections.settings["default"])
            self.assertEqual(connections["default"].settings_dict["NAME"], str(paths.tenant_database(self.alpha)))

    def test_the_copy_carries_the_configured_defaults(self):
        base = connections.settings["default"]
        with bound_tenant(self.alpha):
            copy = connections["default"].settings_dict
        for key in ("ENGINE", "TIME_ZONE", "CONN_MAX_AGE", "AUTOCOMMIT", "ATOMIC_REQUESTS", "OPTIONS", "TEST"):
            self.assertEqual(copy[key], base[key], key)

    def test_binding_another_tenant_inside_one_is_refused(self):
        with bound_tenant(self.alpha):
            wrapper = connections["default"]
            with self.assertRaises(TenancyError):
                with bound_tenant(self.beta):
                    pass  # pragma: no cover
            self.assertIs(connections["default"], wrapper)
            self.assertEqual(current_tenant(), self.alpha)

    def test_the_same_tenant_nests_as_a_no_op(self):
        with bound_tenant(self.alpha):
            wrapper = connections["default"]
            with bound_tenant(Tenant.objects.get(pk=self.alpha.pk)):
                self.assertIs(connections["default"], wrapper)
                Supplier.objects.create(code="T-NESTED", name="Grossiste Imbriqué")
            self.assertIs(connections["default"], wrapper)
            self.assertTrue(Supplier.objects.filter(code="T-NESTED").exists())

    def test_binding_inside_an_open_transaction_is_refused(self):
        with transaction.atomic():
            with self.assertRaises(TenancyError):
                with bound_tenant(self.alpha):
                    pass  # pragma: no cover
        self.assertIsNone(current_tenant())

    def test_everything_is_restored_when_the_block_raises(self):
        before = connections["default"]
        with self.assertRaises(ZeroDivisionError):
            with bound_tenant(self.alpha):
                inner = connections["default"]
                Supplier.objects.count()
                1 / 0  # noqa: B018 - raises on purpose, inside the binding
        self.assertIs(connections["default"], before)
        self.assertIsNone(current_tenant())
        # The tenant's connection was closed, not left to the garbage collector.
        self.assertIsNone(inner.connection)

    def test_a_missing_database_is_refused_rather_than_created_empty(self):
        paths.tenant_database(self.alpha).unlink()
        with self.assertRaises(TenancyError):
            with bound_tenant(self.alpha):
                pass  # pragma: no cover
        self.assertFalse(paths.tenant_database(self.alpha).exists())

    def test_a_binding_needs_a_real_tenant(self):
        with self.assertRaises(TenancyError):
            with bound_tenant(None):
                pass  # pragma: no cover
        self.assertIsNone(current_tenant())

    def test_unbound_there_is_no_tenant(self):
        self.assertIsNone(current_tenant())
        with self.assertRaises(NoTenantBound):
            require_tenant()
        with self.assertRaises(NoTenantBound):
            tenant_key()

    def test_tenant_key_tells_tenants_apart(self):
        with bound_tenant(self.alpha):
            alpha = tenant_key()
        with bound_tenant(self.beta):
            beta = tenant_key()
        self.assertEqual(alpha, str(self.alpha.pk))
        self.assertNotEqual(alpha, beta)

    def test_the_connectors_in_every_bound_tenant_the_server_s_accounts_in_the_owner_s(self):
        owner = self.make_tenant("Bar du Propriétaire", owner=True)
        self.assertFalse(integrations_allowed())
        self.assertFalse(server_accounts_allowed())
        with bound_tenant(self.alpha):
            self.assertTrue(integrations_allowed())
            self.assertFalse(server_accounts_allowed())
        with bound_tenant(owner):
            self.assertTrue(integrations_allowed())
            self.assertTrue(server_accounts_allowed())


class BoundThreadTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_tenant("Bar Alpha")
        self.beta = self.make_tenant("Bar Beta")
        with bound_tenant(self.alpha):
            Supplier.objects.create(code="T-ALPHA", name="Grossiste Alpha")
        with bound_tenant(self.beta):
            Supplier.objects.create(code="T-BETA", name="Grossiste Beta")

    def run_in_thread(self, target, *args):
        errors = []

        def guarded(*inner):
            try:
                target(*inner)
            except BaseException as exc:  # noqa: BLE001 - reported by the main thread
                errors.append(exc)

        thread = threading.Thread(target=guarded, args=args)
        thread.start()
        thread.join(60)
        self.assertEqual(errors, [])

    def test_a_thread_started_with_bound_works_for_the_starting_tenant(self):
        seen, wrappers = [], []

        def job(code):
            seen.append((current_tenant().pk, list(Supplier.objects.values_list("code", flat=True).filter(code=code))))
            wrappers.append(connections["default"])

        with bound_tenant(self.beta):
            target = bound(job)
        self.run_in_thread(target, "T-BETA")
        self.assertEqual(seen, [(self.beta.pk, ["T-BETA"])])
        # The thread's connection was closed at its end.
        self.assertIsNone(wrappers[0].connection)

    def test_a_thread_started_without_bound_works_for_nobody(self):
        seen = []
        with bound_tenant(self.alpha):
            self.run_in_thread(lambda: seen.append(current_tenant()))
        self.assertEqual(seen, [None])

    def test_a_heartbeat_started_from_a_bound_thread_inherits_its_tenant(self):
        seen = []

        def heartbeat():
            seen.append((current_tenant().pk, Supplier.objects.get(code="T-ALPHA").name))

        def worker():
            beat = threading.Thread(target=bound(heartbeat))
            beat.start()
            beat.join(60)

        with bound_tenant(self.alpha):
            self.run_in_thread(bound(worker))
        self.assertEqual(seen, [(self.alpha.pk, "Grossiste Alpha")])

    def test_capturing_nothing_is_refused_in_the_request_itself(self):
        with self.assertRaises(NoTenantBound):
            bound(lambda: None)

    def test_run_in_the_thread_that_made_it_nests_and_closes_nothing(self):
        """What a test does with a patched Thread: call its target."""
        with bound_tenant(self.alpha):
            wrapper = connections["default"]
            Supplier.objects.count()
            result = bound(lambda: (current_tenant().pk, connections["default"]))()
            self.assertEqual(result, (self.alpha.pk, wrapper))
            self.assertIsNotNone(wrapper.connection)

    def test_the_wrapped_target_is_reachable(self):
        def job():
            pass

        with bound_tenant(self.alpha):
            target = bound(job)
        self.assertIs(target.__wrapped__, job)
        self.assertEqual(target.tenant, self.alpha)
