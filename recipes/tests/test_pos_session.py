"""Tests for the L'Addition sign-in.

No browser is started and nothing leaves the machine: Selenium is replaced by
a fake driver, so what's under test is the LOGIC around the login - which
field gets which credential, which of the two submit buttons is clicked, and
what happens when it doesn't work.

That second one is worth pinning down. The login form has two `type=submit`
buttons, "Valider" and "Mot de passe oublié ?", so a selector that matches on
type alone can fire a password reset instead of a sign-in.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from recipes.pos import laddition_session
from recipes.pos.laddition_session import (
    IDENTIFIER_FIELD,
    PASSWORD_FIELD,
    SUBMIT_BUTTON,
    LadditionAuthError,
    log_in,
    open_report,
)


class FakeElement:
    def __init__(self, name):
        self.name = name
        self.typed = []
        self.clicks = 0
        self.text = "x" * 500  # enough body text to count as "the page loaded"

    def send_keys(self, value):
        self.typed.append(value)

    def click(self):
        self.clicks += 1

    def is_enabled(self):
        return True


class FakeDriver:
    """Stands in for a Chrome session. `urls` is the sequence of URLs
    current_url reports, one per read, so a redirect can be simulated."""

    def __init__(self, urls=None, missing=()):
        self._urls = list(urls or ["https://auth.laddition.com/"])
        self.visited = []
        self.elements = {}
        self.missing = set(missing)

    @property
    def current_url(self):
        return self._urls[0] if len(self._urls) == 1 else self._urls.pop(0)

    def get(self, url):
        self.visited.append(url)

    def find_element(self, by, value):
        key = (by, value)
        if key in self.missing:
            from selenium.common.exceptions import NoSuchElementException

            raise NoSuchElementException(str(key))
        return self.elements.setdefault(key, FakeElement(value))


def fake_wait(driver, timeout):
    """A WebDriverWait that resolves conditions immediately, and raises the
    real TimeoutException when one is never satisfied."""

    class _Wait:
        def until(self, condition):
            from selenium.common.exceptions import TimeoutException

            for _ in range(5):
                try:
                    result = condition(driver)
                except Exception:  # noqa: BLE001 - mirrors WebDriverWait's own swallowing
                    result = False
                if result:
                    return result
            raise TimeoutException("condition never became true")

    return _Wait()


@override_settings(LADDITION_EMAIL="bar@example.com", LADDITION_PASSWORD="hunter2")
class LogInTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(laddition_session, "WebDriverWait", fake_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_credentials_go_into_the_right_fields(self):
        driver = FakeDriver(urls=["https://auth.laddition.com/", "https://reporting.laddition.com/v2/x"])
        log_in(driver, log=lambda *a: None)
        self.assertEqual(driver.elements[IDENTIFIER_FIELD].typed, ["bar@example.com"])
        self.assertEqual(driver.elements[PASSWORD_FIELD].typed, ["hunter2"])

    def test_the_validate_button_is_the_one_clicked(self):
        """Not just any submit button - the next one along resets the
        password."""
        driver = FakeDriver(urls=["https://auth.laddition.com/", "https://reporting.laddition.com/v2/x"])
        log_in(driver, log=lambda *a: None)
        self.assertEqual(driver.elements[SUBMIT_BUTTON].clicks, 1)

    def test_the_button_is_matched_on_exact_text_not_on_its_type(self):
        """Neither button on that form carries a `type` ATTRIBUTE - <button>
        merely defaults to submit as a DOM property, so get_attribute("type")
        says "submit" while the XPath `@type` matches nothing at all. And the
        match has to be exact, or it can catch "Mot de passe oublie ?" and
        fire a password reset instead of a sign-in."""
        selector = SUBMIT_BUTTON[1]
        self.assertNotIn("@type", selector)
        self.assertIn("normalize-space", selector)
        self.assertNotIn("contains", selector)

    def test_missing_credentials_fail_before_a_browser_is_touched(self):
        with override_settings(LADDITION_EMAIL="", LADDITION_PASSWORD=""):
            driver = FakeDriver()
            with self.assertRaises(LadditionAuthError) as caught:
                log_in(driver, log=lambda *a: None)
        self.assertIn(".env", str(caught.exception))
        self.assertEqual(driver.visited, [])

    def test_staying_on_the_auth_host_is_reported_as_a_rejected_sign_in(self):
        driver = FakeDriver(urls=["https://auth.laddition.com/"])
        with self.assertRaises(LadditionAuthError) as caught:
            log_in(driver, log=lambda *a: None)
        self.assertIn("rejected", str(caught.exception).lower())

    def test_the_error_never_repeats_the_credentials(self):
        """An exception ends up in a log or a job record."""
        driver = FakeDriver(urls=["https://auth.laddition.com/"])
        with self.assertRaises(LadditionAuthError) as caught:
            log_in(driver, log=lambda *a: None)
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertNotIn("bar@example.com", str(caught.exception))

    def test_an_already_valid_session_does_not_sign_in_again(self):
        driver = FakeDriver(
            urls=["https://reporting.laddition.com/v2/x"],
            missing=[IDENTIFIER_FIELD],
        )
        log_in(driver, log=lambda *a: None)
        self.assertNotIn(PASSWORD_FIELD, driver.elements)


@override_settings(LADDITION_EMAIL="bar@example.com", LADDITION_PASSWORD="hunter2")
class OpenReportTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(laddition_session, "WebDriverWait", fake_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_live_session_goes_straight_to_the_page(self):
        driver = FakeDriver(urls=["https://reporting.laddition.com/v2/shift-details"])
        open_report(driver, "/v2/shift-details", log=lambda *a: None)
        self.assertEqual(driver.visited, ["https://reporting.laddition.com/v2/shift-details"])

    def test_being_bounced_to_auth_triggers_a_sign_in_then_a_retry(self):
        driver = FakeDriver(
            urls=[
                "https://auth.laddition.com/",                     # landed on auth
                "https://auth.laddition.com/",                     # log_in sees the form
                "https://reporting.laddition.com/v2/shift-details",  # after submitting
                "https://reporting.laddition.com/v2/shift-details",  # after the retry
            ]
        )
        open_report(driver, "/v2/shift-details", log=lambda *a: None)
        self.assertEqual(driver.elements[IDENTIFIER_FIELD].typed, ["bar@example.com"])
        # The report page is requested again once authenticated.
        self.assertEqual(
            driver.visited.count("https://reporting.laddition.com/v2/shift-details"), 2
        )


class NavigationRetryTests(SimpleTestCase):
    """A fresh browser's first navigation intermittently fails to resolve the
    host here, and succeeds immediately on a retry. This runs unattended, so
    one blip must not cost the whole import."""

    def setUp(self):
        self.slept = []

    def flaky_driver(self, failures, code="ERR_NAME_NOT_RESOLVED"):
        from selenium.common.exceptions import WebDriverException

        calls = {"n": 0}

        class Driver:
            visited = []

            def get(self, url):
                calls["n"] += 1
                if calls["n"] <= failures:
                    raise WebDriverException(f"unknown error: net::{code}")
                self.visited.append(url)

        return Driver(), calls

    def test_a_transient_failure_is_retried_and_succeeds(self):
        from recipes.pos.laddition_session import navigate

        driver, calls = self.flaky_driver(failures=2)
        navigate(driver, "https://reporting.laddition.com/x", log=lambda *a: None, sleep=self.slept.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(driver.visited, ["https://reporting.laddition.com/x"])

    def test_the_backoff_grows(self):
        from recipes.pos.laddition_session import navigate

        driver, _ = self.flaky_driver(failures=3)
        navigate(driver, "https://x/", log=lambda *a: None, sleep=self.slept.append)
        self.assertEqual(self.slept, [1, 2, 4])

    def test_it_gives_up_after_the_last_attempt(self):
        from selenium.common.exceptions import WebDriverException

        from recipes.pos.laddition_session import NAVIGATION_ATTEMPTS, navigate

        driver, calls = self.flaky_driver(failures=99)
        with self.assertRaises(WebDriverException):
            navigate(driver, "https://x/", log=lambda *a: None, sleep=self.slept.append)
        self.assertEqual(calls["n"], NAVIGATION_ATTEMPTS)

    def test_a_permanent_error_is_not_retried(self):
        """No point burning four attempts and eight seconds on a crashed
        browser or a bad URL."""
        from selenium.common.exceptions import WebDriverException

        from recipes.pos.laddition_session import navigate

        driver, calls = self.flaky_driver(failures=99, code="ERR_BLOCKED_BY_CLIENT")
        with self.assertRaises(WebDriverException):
            navigate(driver, "https://x/", log=lambda *a: None, sleep=self.slept.append)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, [])


class PathNormalisationTests(SimpleTestCase):
    """Git Bash rewrites any argument that looks like a Unix path into a
    Windows one, so `--path /v2/shift-details` arrives mangled. Pasted onto
    the host it yields ERR_NAME_NOT_RESOLVED, which reads like a network
    fault and sends you looking in completely the wrong place."""

    def normalise(self, value):
        from recipes.pos.laddition_session import normalise_path

        return normalise_path(value)

    def test_a_plain_path_is_unchanged(self):
        self.assertEqual(self.normalise("/v2/shift-details"), "/v2/shift-details")

    def test_a_missing_leading_slash_is_added(self):
        self.assertEqual(self.normalise("v2/shift-details"), "/v2/shift-details")

    def test_the_git_bash_mangling_is_undone(self):
        self.assertEqual(
            self.normalise("C:/Program Files/Git/v2/shift-details"), "/v2/shift-details"
        )

    def test_a_windows_backslash_path_is_undone(self):
        self.assertEqual(
            self.normalise(r"C:\Program Files\Git\v2\z-digital"), "/v2/z-digital"
        )

    def test_a_full_url_is_reduced_to_its_path(self):
        self.assertEqual(
            self.normalise("https://reporting.laddition.com/v2/shift-details"), "/v2/shift-details"
        )

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(self.normalise("  /v2/shift-details  "), "/v2/shift-details")

    def test_a_path_with_a_query_string_survives(self):
        self.assertEqual(
            self.normalise("C:/Program Files/Git/v2/shift-details?from=2026-01-01"),
            "/v2/shift-details?from=2026-01-01",
        )

    def test_an_empty_path_becomes_the_root(self):
        self.assertEqual(self.normalise(""), "/")
