"""Which tenant an employee's public signing link belongs to
(accounts/links.py)."""

from accounts import links
from accounts.models import SigningLink, hash_secret
from accounts.tenancy import NoTenantBound, TenancyError, bound_tenant
from accounts.tests.support import TwoTenantsTestCase

# Invented tokens: only their hashes are ever stored.
ALPHA = hash_secret("jeton-essai-alpha")
BETA = hash_secret("jeton-essai-beta")


class MultiModeLinksTests(TwoTenantsTestCase):
    def test_a_link_resolves_to_the_tenant_that_issued_it(self):
        with bound_tenant(self.bar_a):
            links.register(ALPHA)
        with bound_tenant(self.bar_b):
            links.register(BETA)
        self.assertEqual(links.resolve(ALPHA), self.bar_a)
        self.assertEqual(links.resolve(BETA), self.bar_b)
        self.assertIsNone(links.resolve(hash_secret("jeton-inconnu")))
        self.assertIsNone(links.resolve(""))

    def test_registering_twice_is_harmless(self):
        with bound_tenant(self.bar_a):
            links.register(ALPHA)
            links.register(ALPHA)
        self.assertEqual(SigningLink.objects.filter(token_hash=ALPHA).count(), 1)

    def test_another_tenant_cannot_take_a_link_over(self):
        with bound_tenant(self.bar_a):
            links.register(ALPHA)
        with bound_tenant(self.bar_b), self.assertRaises(TenancyError):
            links.register(ALPHA)
        self.assertEqual(links.resolve(ALPHA), self.bar_a)

    def test_forgetting_removes_the_bound_tenant_s_links_only(self):
        with bound_tenant(self.bar_a):
            links.register(ALPHA)
        with bound_tenant(self.bar_b):
            self.assertEqual(links.forget(ALPHA, "", BETA), 0)
        self.assertEqual(links.resolve(ALPHA), self.bar_a)
        with bound_tenant(self.bar_a):
            self.assertEqual(links.forget(ALPHA), 1)
        self.assertIsNone(links.resolve(ALPHA))

    def test_a_closed_tenant_s_links_open_nothing(self):
        with bound_tenant(self.bar_a):
            links.register(ALPHA)
        self.bar_a.is_active = False
        self.bar_a.save(update_fields=["is_active"])
        self.assertIsNone(links.resolve(ALPHA))

    def test_unbound_nothing_is_registered(self):
        with self.assertRaises(NoTenantBound):
            links.register(ALPHA)
        with self.assertRaises(NoTenantBound):
            links.forget(ALPHA)
        self.assertFalse(SigningLink.objects.exists())

    def test_registering_inside_the_tenant_s_transaction(self):
        """The staff code issues a link inside its own transaction on the
        tenant's database: the index is another database, written at once."""
        from django.db import transaction

        with bound_tenant(self.bar_a):
            with transaction.atomic():
                links.register(ALPHA)
                self.assertEqual(links.resolve(ALPHA), self.bar_a)
