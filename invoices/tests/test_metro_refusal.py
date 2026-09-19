"""Metro's firewall, and what the scraper does when a run goes wrong.

Metro's edge firewall refused the sign-in on 02/09 (twice) and 18/09, at the
moment the credentials were sent: « Vous avez été bloqué par notre pare-feu.
Veuillez contacter notre assistance Metro et communiquer cet identifiant :
#18.… ». The scraper never looked: it waited 15 s for the date filters,
blamed a cookie popup or a new layout - which reads like a glitch to retry -
and the gather failed as a whole. Every sign-in against a block is one more
refused sign-in. Here: the refusal is recognised wherever it can show, said
as such with its reference, and never retried; the browser is restarted only
when it really died; what was downloaded before a stop is kept.

A fake browser, never Metro. Data and references invented.
"""

import os
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, StaleElementReferenceException

from invoices.scrapers import metro

BANNER = (
    "×\nVous avez été bloqué par notre pare-feu. Veuillez contacter notre assistance Metro et communiquer cet "
    "identifiant :#18.0000000.1700000000.00000abc\nMes Factures METRO\nEmail\nMot de passe\nMe connecter"
)


class RefusalTextTests(SimpleTestCase):
    def test_the_firewall_page_is_a_refusal_with_its_reference(self):
        self.assertEqual(metro.blocked_reference(BANNER), "#18.0000000.1700000000.00000abc")

    def test_other_refusals_are_refusals_too(self):
        self.assertEqual(metro.blocked_reference("429 Too Many Requests"), "")
        self.assertEqual(metro.blocked_reference("Access Denied - You don't have permission"), "")

    def test_an_ordinary_page_is_not(self):
        self.assertIsNone(metro.blocked_reference("Mes Factures\nDe\nÀ\nRechercher\nFactures/page"))
        self.assertIsNone(metro.blocked_reference(""))

    def test_the_message_says_not_to_retry_and_whom_to_ask(self):
        message = metro.blocked_message("#18.0000000.1700000000.00000abc")
        self.assertIn("pare-feu", message)
        self.assertIn("#18.0000000.1700000000.00000abc", message)
        self.assertIn("assistance Metro", message)


class Page:
    """A fake browser page: its visible text, whether the invoice filters
    are on it, whether the sign-in form still is."""

    def __init__(self, text="", signed_in=False, login_form=False):
        self.text = text
        self.signed_in = signed_in
        self.login_form = login_form


class FakeBrowser:
    current_url = "https://docs.metro.fr/"
    title = "Metro"

    def __init__(self, pages):
        self.pages = list(pages)  # one per poll; the last one stays

    @property
    def page(self):
        return self.pages[0]

    def advance(self):
        if len(self.pages) > 1:
            self.pages.pop(0)

    def find_element(self, by, value):
        return self.page

    def find_elements(self, by, value):
        if value == metro.DATE_FROM_SELECTOR:
            return [object()] if self.page.signed_in else []
        if value == "password":
            return [object()] if self.page.login_form else []
        return []

    def save_screenshot(self, path):
        return False


class SignInOutcomeTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def await_sign_in(self, browser):
        return metro._await_sign_in(browser, self.dir, lambda message: None, sleep=lambda s: browser.advance())

    def test_signed_in_is_the_invoice_filters_appearing(self):
        self.await_sign_in(FakeBrowser([Page("Connexion…"), Page("Mes Factures", signed_in=True)]))

    def test_the_firewall_answering_is_said_at_once_never_waited_out(self):
        browser = FakeBrowser([Page("Connexion…"), Page(BANNER)])
        with self.assertRaises(metro.MetroBlocked) as raised:
            self.await_sign_in(browser)
        self.assertEqual(raised.exception.reference, "#18.0000000.1700000000.00000abc")

    def test_a_refused_password_is_not_the_firewall(self):
        """No long pause for a password to fix - but no retry either."""
        browser = FakeBrowser([Page("Identifiant ou mot de passe incorrect", login_form=True)])
        browser.current_url = "https://idam.metro.fr/web/Signin?client_id=EINVOICE"
        with mock.patch.object(metro, "PAGE_WAIT_SECONDS", 0.2), mock.patch.object(metro, "POLL_SECONDS", 0.05):
            with self.assertRaises(metro.MetroLoginFailed) as raised:
                metro._await_sign_in(browser, self.dir, lambda message: None)
        self.assertNotIsInstance(raised.exception, metro.MetroBlocked)
        self.assertIn("METRO_PASSWORD", str(raised.exception))


class LoginTests(TestCase):
    """The sign-in is noted before the password is sent: a run that dies
    after it still counts against the day's one sign-in."""

    def test_the_sign_in_is_noted_before_the_password_goes(self):
        from invoices.models import Supplier

        noted_at_submit = []

        class Field:
            def __init__(self, on_click=None):
                self.on_click = on_click

            def send_keys(self, *keys):
                pass

            def click(self):
                if self.on_click:
                    self.on_click()

        browser = FakeBrowser([Page("Me connecter", login_form=True)])

        def submit():
            noted_at_submit.append(Supplier.objects.get(code="METRO").scrape_last_login_at)
            browser.pages = [Page("Mes Factures", signed_in=True)]

        fields = {"user_id": Field(), "password": Field(), "submit": Field(submit)}

        def find_element(by, value):
            if value == "cms-cookie-disclaimer":
                raise LookupError("no cookie banner on this page")
            return fields.get(value) or browser.page

        browser.get = lambda url: None
        browser.find_element = find_element

        class Wait:
            def until(self, condition):
                from selenium.common.exceptions import TimeoutException

                try:
                    found = condition(browser)
                except Exception:  # noqa: BLE001 - as WebDriverWait ignores a missing element
                    found = None
                if not found:
                    raise TimeoutException("fake")
                return found

        with override_settings(METRO_EMAIL="acheteur@exemple.fr", METRO_PASSWORD="secret"):
            metro._login(browser, Wait(), tempfile.mkdtemp(), lambda message: None)
        self.assertIsNotNone(noted_at_submit[0])

        # Cancelled before the credentials go: nothing sent, nothing noted.
        Supplier.objects.filter(code="METRO").update(scrape_last_login_at=None)
        browser.pages = [Page("Me connecter", login_form=True)]
        with override_settings(METRO_EMAIL="acheteur@exemple.fr", METRO_PASSWORD="secret"):
            with self.assertRaises(metro._MetroCancelled):
                metro._login(browser, Wait(), tempfile.mkdtemp(), lambda message: None, should_cancel=lambda: True)
        self.assertEqual(len(noted_at_submit), 1)
        self.assertIsNone(Supplier.objects.get(code="METRO").scrape_last_login_at)


class PauseTests(TestCase):
    """Metro left alone after a refusal, and signed in to at most once a day
    - kept on the supplier, where every caller of the scraper reads it."""

    def setUp(self):
        from invoices.models import Supplier

        self.metro = Supplier.objects.get(code="METRO")
        self.now = timezone.now()

    def test_a_refusal_pauses_metro_for_a_week(self):
        metro.record_block("#18.1", now=self.now)
        self.metro.refresh_from_db()
        self.assertEqual(self.metro.scrape_paused_until, self.now + metro.BLOCK_PAUSE)
        self.assertIn("#18.1", self.metro.scrape_pause_reason)
        paused = metro.metro_pause(now=self.now + timedelta(days=1))
        self.assertTrue(paused.after_block)
        self.assertIn("#18.1", str(paused))
        self.assertIsNone(metro.metro_pause(now=self.now + metro.BLOCK_PAUSE + timedelta(minutes=1)))

    def test_a_second_refusal_within_a_month_pauses_it_twice_as_long(self):
        metro.record_block("#18.1", now=self.now - timedelta(days=16))
        metro.record_block("#18.2", now=self.now)
        self.metro.refresh_from_db()
        self.assertEqual(self.metro.scrape_paused_until, self.now + 2 * metro.BLOCK_PAUSE)

    def test_a_sign_in_less_than_a_day_old_holds_the_next(self):
        metro.record_login(now=self.now)
        paused = metro.metro_pause(now=self.now + timedelta(hours=3))
        self.assertFalse(paused.after_block)
        self.assertEqual(paused.until, self.now + metro.MIN_GAP_BETWEEN_LOGINS)
        self.assertIsNone(metro.metro_pause(now=self.now + metro.MIN_GAP_BETWEEN_LOGINS + timedelta(minutes=1)))


class FakeDriver:
    """Enough of a browser for scrape_metro_invoices once _login and the
    filter are replaced: no rows, an ordinary page."""

    def __init__(self):
        self.quit_calls = 0

    def find_element(self, by, value):
        return Page("Mes Factures")

    def find_elements(self, by, value):
        return []

    def quit(self):
        self.quit_calls += 1


@override_settings(METRO_EMAIL="acheteur@exemple.fr", METRO_PASSWORD="secret")
class RunTests(TestCase):
    """The run around the windows: restarts, refusals, what landed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.drivers = []
        for name, value in (("PAGE_WAIT_SECONDS", 0.2), ("POLL_SECONDS", 0.05), ("RESTART_PAUSE_SECONDS", 0)):
            patcher = mock.patch.object(metro, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, download_dir):
        driver = FakeDriver()
        self.drivers.append(driver)
        return driver

    def scrape(self, login=None, apply_filter=None, start=date(2026, 1, 1), end=date(2026, 9, 18)):
        with mock.patch.object(metro, "_build_driver", side_effect=self.build), mock.patch.object(
            metro, "_login", side_effect=login or (lambda *args, **kwargs: None)
        ), mock.patch.object(metro, "_apply_date_filter", side_effect=apply_filter or (lambda *args, **kwargs: None)):
            return metro.scrape_metro_invoices(self.dir, start, end, log=lambda message: None)

    def test_a_refused_sign_in_is_never_tried_again(self):
        def refused(*args, **kwargs):
            raise metro.MetroBlocked(metro.blocked_message("#18.1"), reference="#18.1")

        with self.assertRaises(metro.MetroBlocked):
            self.scrape(login=refused)
        self.assertEqual(len(self.drivers), 1)
        self.assertEqual(self.drivers[0].quit_calls, 1)
        # ... and Metro is left alone from now on, whoever calls the scraper.
        self.assertTrue(metro.metro_pause().after_block)
        with self.assertRaises(metro.MetroPaused):
            self.scrape()
        self.assertEqual(len(self.drivers), 1, "a browser was started for a paused Metro")

    def test_a_person_can_ask_for_one_sign_in_all_the_same(self):
        metro.record_block("#18.1")
        with mock.patch.object(metro, "_build_driver", side_effect=self.build), mock.patch.object(
            metro, "_login"
        ), mock.patch.object(metro, "_apply_date_filter"):
            metro.scrape_metro_invoices(
                self.dir, date(2026, 9, 1), date(2026, 9, 18), log=lambda message: None, ignore_pause=True
            )
        self.assertEqual(len(self.drivers), 1)

    def test_a_page_not_as_expected_is_not_a_reason_to_sign_in_again(self):
        """A stale element, a click intercepted: the page, not the browser.
        Each one used to open a new browser and sign in again, three
        sign-ins in seconds."""

        def stale(*args, **kwargs):
            raise StaleElementReferenceException("stale element reference")

        with self.assertRaises(metro.MetroError):
            self.scrape(apply_filter=stale)
        self.assertEqual(len(self.drivers), 1)

    def test_a_browser_that_died_is_restarted_once(self):
        calls = []

        def dies_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise InvalidSessionIdException("invalid session id")

        self.scrape(apply_filter=dies_once)
        self.assertEqual(len(self.drivers), 2)

    def test_a_browser_dying_again_stops_the_run_and_says_so(self):
        def always_dies(*args, **kwargs):
            raise InvalidSessionIdException("invalid session id")

        with self.assertRaises(metro.MetroError) as raised:
            self.scrape(apply_filter=always_dies)
        self.assertEqual(len(self.drivers), 1 + metro.MAX_SESSION_RESTARTS)
        self.assertIn("2026-01-01", str(raised.exception))

    def test_a_window_closed_by_hand_stops_the_run(self):
        def closed(*args, **kwargs):
            raise NoSuchWindowException("no such window: target window already closed")

        with self.assertRaises(metro.MetroError):
            self.scrape(apply_filter=closed)
        self.assertEqual(len(self.drivers), 1)

    def test_what_landed_before_a_stop_comes_with_it(self):
        """Not imported, it was fetched from Metro again at the next run."""
        windows = []

        def second_window_fails(driver, wait, download_dir, log, start, end):
            windows.append(start)
            if len(windows) == 1:
                with open(os.path.join(download_dir, "134_52_14645_20260122072041_invoice_cus_copy_main.pdf"), "wb") as handle:
                    handle.write(b"%PDF-1.4")
            else:
                raise StaleElementReferenceException("stale element reference")

        with self.assertRaises(metro.MetroError) as raised:
            self.scrape(apply_filter=second_window_fails)
        self.assertEqual([os.path.basename(path) for path in raised.exception.files],
                         ["134_52_14645_20260122072041_invoice_cus_copy_main.pdf"])

    def test_a_cancel_before_the_start_signs_in_to_nothing(self):
        with mock.patch.object(metro, "_build_driver", side_effect=self.build), mock.patch.object(
            metro, "_login"
        ) as login:
            metro.scrape_metro_invoices(
                self.dir, date(2026, 9, 1), date(2026, 9, 18), log=lambda message: None, should_cancel=lambda: True
            )
        self.assertEqual((self.drivers, login.call_count), ([], 0))

    def test_a_refusal_is_said_even_if_its_pause_cannot_be_written(self):
        def refused(*args, **kwargs):
            raise metro.MetroBlocked(metro.blocked_message("#18.1"), reference="#18.1")

        with mock.patch.object(metro, "record_block", side_effect=RuntimeError("base verrouillée")):
            with self.assertRaises(metro.MetroBlocked):
                self.scrape(login=refused)

    def test_a_download_cut_off_by_a_dying_browser_is_fetched_after_the_restart(self):
        """Marked fetched at its click, it was skipped after the restart as
        "already imported", and never imported."""
        import threading

        state = {"clicks_on_1": 0, "clicks_on_2": 0, "died": False}
        folder = self.dir

        def land(name):
            """A moment after the click, as a real download lands."""

            def write():
                with open(os.path.join(folder, f"{name}_20260122072041_invoice_cus_copy_main.pdf"), "wb") as handle:
                    handle.write(b"%PDF-1.4")

            threading.Timer(0.1, write).start()

        class RowsDriver(FakeDriver):
            def find_elements(self, by, value):
                return [object(), object()]

            def execute_script(self, script, *args):
                if script == metro.ROWS_JS:
                    if state["clicks_on_2"] == 1 and not state["died"]:
                        state["died"] = True
                        raise InvalidSessionIdException("invalid session id")
                    return [("r1", "FRA_134_52_1_20260122072041"), ("r2", "FRA_134_52_2_20260122072041")]
                if args[0] == "r1":
                    state["clicks_on_1"] += 1
                    land("134_52_1")
                else:
                    state["clicks_on_2"] += 1
                    if state["clicks_on_2"] > 1:
                        land("134_52_2")

        def build(download_dir):
            driver = RowsDriver()
            self.drivers.append(driver)
            return driver

        with mock.patch.object(metro, "_build_driver", side_effect=build), mock.patch.object(metro, "_login"), \
                mock.patch.object(metro, "_apply_date_filter"), mock.patch.object(metro, "CLICK_INTERVAL_SECONDS", 0), \
                mock.patch.object(metro, "_wait_for_stable_results", return_value=2):
            files = metro.scrape_metro_invoices(self.dir, date(2026, 9, 1), date(2026, 9, 18), log=lambda message: None)
        self.assertEqual(len(self.drivers), 2)
        self.assertEqual(sorted(os.path.basename(path)[:12] for path in files), ["134_52_1_202", "134_52_2_202"])
        self.assertEqual((state["clicks_on_1"], state["clicks_on_2"]), (1, 2))

    def test_an_unreadable_row_is_said_once_every_window_was_searched(self):
        searched = []

        class OddRowDriver(FakeDriver):
            def find_elements(self, by, value):
                return [object()] if len(searched) == 1 else []

            def execute_script(self, script, *args):
                return [("odd", "sans-numero")] if len(searched) == 1 else []

        def build(download_dir):
            driver = OddRowDriver()
            self.drivers.append(driver)
            return driver

        with mock.patch.object(metro, "_build_driver", side_effect=build), mock.patch.object(metro, "_login"), \
                mock.patch.object(metro, "_apply_date_filter", side_effect=lambda *a: searched.append(a[4])), \
                mock.patch.object(metro, "_wait_for_stable_results", return_value=1):
            with self.assertRaises(metro.MetroError) as raised:
                metro.scrape_metro_invoices(self.dir, date(2026, 1, 1), date(2026, 9, 18), log=lambda message: None)
        self.assertEqual(len(searched), 3, "the windows after the odd row were never searched")
        self.assertIn("2026-01-01", str(raised.exception))

    def test_a_file_landed_just_before_the_browser_died_is_not_fetched_again(self):
        import threading

        clicks = {"r1": 0, "r2": 0}
        state = {"died": False}
        folder = self.dir

        def land(name):
            def write():
                with open(os.path.join(folder, f"{name}_20260122072041_invoice_cus_copy_main.pdf"), "wb") as handle:
                    handle.write(b"%PDF-1.4")

            threading.Timer(0.05, write).start()

        class DiesAfterFirstClick(FakeDriver):
            def find_elements(self, by, value):
                return [object(), object()]

            def execute_script(self, script, *args):
                if script == metro.ROWS_JS:
                    if clicks["r1"] == 1 and not state["died"]:
                        state["died"] = True
                        raise InvalidSessionIdException("invalid session id")
                    return [("r1", "FRA_134_52_1_20260122072041"), ("r2", "FRA_134_52_2_20260122072041")]
                clicks[args[0]] += 1
                land("134_52_1" if args[0] == "r1" else "134_52_2")

        def build(download_dir):
            driver = DiesAfterFirstClick()
            self.drivers.append(driver)
            return driver

        with mock.patch.object(metro, "_build_driver", side_effect=build), mock.patch.object(metro, "_login"), \
                mock.patch.object(metro, "_apply_date_filter"), mock.patch.object(metro, "CLICK_INTERVAL_SECONDS", 0.3), \
                mock.patch.object(metro, "_wait_for_stable_results", return_value=2):
            metro.scrape_metro_invoices(self.dir, date(2026, 9, 1), date(2026, 9, 18), log=lambda message: None)
        self.assertEqual(clicks, {"r1": 1, "r2": 1})

    def test_it_can_be_cancelled_between_windows(self):
        windows = []

        def count(driver, wait, download_dir, log, start, end):
            windows.append(start)

        with mock.patch.object(metro, "_build_driver", side_effect=self.build), mock.patch.object(
            metro, "_login"
        ), mock.patch.object(metro, "_apply_date_filter", side_effect=count):
            metro.scrape_metro_invoices(
                self.dir, date(2026, 1, 1), date(2026, 9, 18), log=lambda message: None,
                should_cancel=lambda: len(windows) >= 1,
            )
        self.assertEqual(len(windows), 1)
