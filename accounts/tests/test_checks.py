"""accounts.E005 (accounts/checks.py): at most ONE active tenant uses the
server's own accounts - one Metro account, one pause.

Metro's pause (24 hours between two sign-ins, a week after a refusal) lives
on the METRO row of the tenant that signs in. Two tenants with
`uses_server_integrations` would each keep a pause of their own and sign in
to the ONE account twice as often - how its firewall blocks the owner - and
two bars would share his mailbox and his till. Names invented."""

import tempfile
import warnings
from pathlib import Path

from django.core.checks import Error, run_checks
from django.test import SimpleTestCase, TestCase, override_settings

from accounts import checks
from accounts.models import Tenant
from accounts.tests.support import TenancyTestCase


class OneOwnerTenantTests(TenancyTestCase):
    def test_one_tenant_with_the_server_s_accounts_is_the_rule(self):
        self.make_tenant("Bar du Propriétaire", owner=True)
        self.make_tenant("Bar Voisin")
        self.assertEqual(checks.one_owner_tenant(), [])

    def test_none_at_all_is_fine_too(self):
        self.make_tenant("Bar Voisin")
        self.assertEqual(checks.one_owner_tenant(), [])

    def test_two_active_ones_are_refused_and_named(self):
        first = self.make_tenant("Bar du Propriétaire", owner=True)
        second = self.make_tenant("Bar Copié", owner=True)
        (issue,) = checks.one_owner_tenant()
        self.assertIsInstance(issue, Error)
        self.assertEqual(issue.id, "accounts.E005")
        for tenant in (first, second):
            self.assertIn(tenant.dir_name, issue.msg)
            self.assertIn(tenant.name, issue.msg)
        # Registered: every runserver and `manage.py check` refuse.
        self.assertIn("accounts.E005", [message.id for message in run_checks()])

    def test_a_closed_one_does_not_count(self):
        self.make_tenant("Bar du Propriétaire", owner=True)
        old = self.make_tenant("Ancien Bar", owner=True)
        Tenant.objects.filter(pk=old.pk).update(is_active=False)
        self.assertEqual(checks.one_owner_tenant(), [])


class LimiterCacheTests(TestCase):
    """accounts.W002: the login limiter (accounts/limiter.py) counts with the
    cache's `add` then `incr`, and relies on `incr` being atomic and keeping
    the key's expiry. The file and database caches re-set the key with
    their default TIMEOUT (5 minutes) at every `incr` - ten failures then
    held a login back 5 minutes from the latest, not 15 from the first -
    and are not atomic across processes; the dummy cache keeps nothing, so
    nothing is ever held back.

    A TestCase: `run_checks` also runs accounts.E005, which reads the
    accounts database."""

    def issues(self, backend):
        folder = tempfile.mkdtemp(prefix="marginmate-tests-cache-")
        caches = {"default": {"BACKEND": backend, "LOCATION": folder}}
        with override_settings(CACHES=caches):
            return [issue for issue in checks.limiter_cache() if issue.id == "accounts.W002"]

    def test_a_cache_the_limiter_cannot_count_in_is_warned_about(self):
        for backend in (
            "django.core.cache.backends.filebased.FileBasedCache",
            "django.core.cache.backends.db.DatabaseCache",
            "django.core.cache.backends.dummy.DummyCache",
        ):
            with self.subTest(backend=backend):
                (issue,) = self.issues(backend)
                self.assertIn(backend, issue.msg)
                self.assertIn("Redis", issue.hint)

    def test_memory_redis_and_memcached_are_fine(self):
        for backend in (
            "django.core.cache.backends.locmem.LocMemCache",
            "django.core.cache.backends.redis.RedisCache",
            "django.core.cache.backends.memcached.PyMemcacheCache",
        ):
            with self.subTest(backend=backend):
                self.assertEqual(self.issues(backend), [])

    def test_it_is_registered(self):
        folder = tempfile.mkdtemp(prefix="marginmate-tests-cache-")
        caches = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": folder}}
        with override_settings(CACHES=caches):
            self.assertIn("accounts.W002", [issue.id for issue in run_checks()])


class OneOwnerTenantWithoutDatabaseTests(SimpleTestCase):
    """Never an accounts database created by looking for one (SQLite makes
    the file it opens)."""

    def test_an_accounts_database_not_made_yet_is_left_unmade(self):
        missing = Path(tempfile.mkdtemp(prefix="marginmate-tests-checks-")) / "comptes.sqlite3"
        memory = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        with warnings.catch_warnings():
            # Overriding DATABASES warns: only this check reads it here.
            warnings.simplefilter("ignore")
            with override_settings(
                DATABASES={"default": memory, "accounts": {"ENGINE": memory["ENGINE"], "NAME": str(missing)}},
            ):
                self.assertEqual(checks.one_owner_tenant(), [])
        self.assertFalse(missing.exists())
