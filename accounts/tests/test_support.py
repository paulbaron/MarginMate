"""`TenancyTestCase` (accounts/tests/support.py) leaves the process as it
found it.

For its classes it takes the suite's binding away - tests/runner.py makes
the test espace every thread's by default - and it must put it back when
the class is done, and when its setUpClass fails: left behind, every class
that followed in that process would run unbound, and fail far from the one
that caused it."""

import unittest
from unittest import mock

from django.test.runner import DiscoverRunner

from accounts import tenancy
from accounts.tests.support import TenancyTestCase
from tests.runner import TEST_TENANT_PK


def probe_class():
    class Probe(TenancyTestCase):
        def test_nothing(self):
            pass

    return Probe


class LeavesNothingBehindTests(unittest.TestCase):
    """A plain unittest case on purpose: Django's own class fixtures
    (SimpleTestCase's) wrap the connections this looks at."""

    def test_the_suite_s_binding_comes_back_after_the_class(self):
        probe = probe_class()
        before = tenancy._current
        probe.setUpClass()
        try:
            self.assertIsNone(tenancy.current_tenant())
        finally:
            probe.tearDownClass()
            probe.doClassCleanups()
        self.assertIs(tenancy._current, before)
        self.assertEqual(tenancy.current_tenant().pk, TEST_TENANT_PK)

    def test_and_when_the_class_fails_to_start(self):
        probe = probe_class()
        before = tenancy._current
        with mock.patch("accounts.tests.support.template_master", side_effect=RuntimeError("disque plein")):
            with self.assertRaises(RuntimeError):
                probe.setUpClass()
        self.assertIs(tenancy._current, before)
        self.assertEqual(tenancy.current_tenant().pk, TEST_TENANT_PK)

    def test_its_tests_name_the_suite_s_two_databases(self):
        """Nothing installed or removed per class: the runner builds both
        test databases before any class starts."""
        suite = unittest.TestSuite([probe_class()("test_nothing")])
        self.assertEqual(set(DiscoverRunner(verbosity=0).get_databases(suite)), {"default", "accounts"})
