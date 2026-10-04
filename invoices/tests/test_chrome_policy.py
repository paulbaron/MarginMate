"""The server's Chrome for another bar (invoices/scrapers/chrome.py): always
headless, and a browser free for it or a refusal at once - two for every
other espace together, one for each. The platform owner's sessions are as
they were. L'Addition's session (recipes/pos/laddition_session.py) follows
both. No browser starts: the driver is replaced. Data invented.
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from unittest import mock

from django.test import override_settings

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices.scrapers import chrome
from recipes.pos import laddition_session as session_module


class ChromePolicyTests(TwoTenantsTestCase):
    """Bar Alpha is the platform owner's espace; Beta, Gamma and Delta other bars."""

    owner_a = True

    def setUp(self):
        super().setUp()
        self.bar_c = self.make_tenant("Bar Gamma")
        self.bar_d = self.make_tenant("Bar Delta")

    def test_another_bar_s_chrome_is_always_headless(self):
        with bound_tenant(self.bar_a):
            self.assertFalse(chrome.headless(False))
            self.assertTrue(chrome.headless(True))
        with bound_tenant(self.bar_b):
            self.assertTrue(chrome.headless(False))
        self.assertTrue(chrome.headless(False))

    def hold(self, tenant) -> ExitStack:
        """A browser taken for `tenant`, held after its binding ends - one
        thread binds one espace at a time, the server's sessions are many."""
        held = ExitStack()
        self.addCleanup(held.close)
        with bound_tenant(tenant):
            held.enter_context(chrome.browser_slot())
        return held

    def refused(self, tenant) -> bool:
        with bound_tenant(tenant):
            try:
                with chrome.browser_slot():
                    return False
            except chrome.BrowsersBusy as busy:
                self.assertEqual(str(busy), chrome.BROWSERS_BUSY)
                return True

    def test_one_browser_for_a_bar_two_for_every_other_bar_none_waited_for(self):
        beta = self.hold(self.bar_b)
        self.assertTrue(self.refused(self.bar_b))
        gamma = self.hold(self.bar_c)
        self.assertEqual(chrome.running(), 2)
        self.assertTrue(self.refused(self.bar_d))
        # The owner's own sessions are not counted, nor limited.
        self.assertFalse(self.refused(self.bar_a))
        with bound_tenant(self.bar_a), chrome.browser_slot(), chrome.browser_slot():
            self.assertEqual(chrome.running(), 2)
        beta.close()
        self.assertFalse(self.refused(self.bar_d))
        gamma.close()
        self.assertEqual(chrome.running(), 0)

    def free(self, tenant) -> bool:
        with bound_tenant(tenant):
            return chrome.slot_free()

    def test_a_free_browser_is_asked_about_without_taking_one(self):
        """`slot_free` answers what `browser_slot` would, and takes nothing:
        a run asks it before it starts (recipes/auto_sales.py)."""
        self.assertTrue(self.free(self.bar_b))
        self.assertEqual(chrome.running(), 0)
        beta = self.hold(self.bar_b)
        self.assertFalse(self.free(self.bar_b))  # its one browser is taken
        self.assertTrue(self.free(self.bar_c))
        gamma = self.hold(self.bar_c)
        self.assertFalse(self.free(self.bar_d))  # every other bar's two are
        # The owner's espace is never held back by the others'.
        self.assertTrue(self.free(self.bar_a))
        beta.close()
        self.assertTrue(self.free(self.bar_d))
        gamma.close()

    def test_a_slot_is_given_back_when_the_run_fails(self):
        with bound_tenant(self.bar_b), self.assertRaises(ValueError), chrome.browser_slot():
            raise ValueError("échec")
        self.assertEqual(chrome.running(), 0)

    def built_options(self, tenant):
        with (
            bound_tenant(tenant),
            override_settings(SCRAPER_HEADLESS=False),
            mock.patch.object(session_module.webdriver, "Chrome") as chrome_class,
            mock.patch.object(session_module, "ChromeDriverManager"),
            mock.patch.object(session_module, "Service"),
        ):
            session_module.build_driver(tempfile.mkdtemp())
        return chrome_class.call_args.kwargs["options"].arguments

    def test_l_addition_s_browser_is_headless_for_another_bar(self):
        self.assertIn("--headless=new", self.built_options(self.bar_b))
        self.assertNotIn("--headless=new", self.built_options(self.bar_a))

    def test_l_addition_s_session_takes_a_browser_or_says_why(self):
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_EMAIL": "caisse-beta@example.invalid", "LADDITION_PASSWORD": "secret-beta"})
            with (
                mock.patch.object(session_module, "build_driver") as build,
                mock.patch.object(session_module, "open_report"),
            ):
                with session_module.laddition_session(tempfile.mkdtemp()):
                    self.assertEqual(chrome.running(), 1)
                    with self.assertRaisesMessage(session_module.LadditionAuthError, chrome.BROWSERS_BUSY):
                        with session_module.laddition_session(tempfile.mkdtemp()):
                            pass
            build.assert_called_once()
        self.assertEqual(chrome.running(), 0)
