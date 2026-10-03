"""/notifications/, its reminders and its alerts in a real (headless) Chrome,
as a 375 × 667 touch phone (SPEC §7): what static/js/notifications.js and the
page's CSS do, which the test client cannot see.

* **It fits the phone**: no sideways scroll on any of the three pages, every
  field 16 px at least (iOS zooms the page in on a smaller one).
* **Thumb-sized**: every button and summary of <main> outside a table 44 px
  tall at least (the touch section of marginmate.css).
* **« Cet appareil »**: once the script has run, exactly one of its states
  is shown, and never « JavaScript requis » - whichever the headless
  browser's permission makes it.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run on its own
with the cached chromedriver. Skipped where Chrome or its driver is missing.
Data invented. Nothing is sent: the test server's DEBUG/SITE_URL keep
`sending_enabled()` false, and the page's « Activer » is never tapped.
"""

import tempfile

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from invoices.scrapers import website
from tests.runner import log_in_the_browser

WIDTH, HEIGHT = 375, 667


@tag("browser")
class NotificationsOnAPhoneInBrowserTests(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        from accounts.models import Membership
        from notifications.models import Dispatch, Reminder
        from notifications.tests.support import make_device
        from tests.runner import TEST_TENANT_PK, test_user

        reminder = Reminder.objects.create(
            name="Vides avant livraison",
            title="Consignes",
            body="Photographiez et comptez les vides avant la livraison.",
            target="/consignes/#new-pickup",
            weekdays="0,2,5",
            times="00:00 02:00",
            skip_if="returnables.recent_pickup",
        )
        Dispatch.objects.create(
            kind=Dispatch.Kind.REMINDER,
            reminder_id=reminder.pk,
            rule_name=reminder.name,
            dedupe_key=f"reminder:{reminder.pk}:20310311T2300Z",
            title="Consignes",
            ttl=3600,
            status=Dispatch.Status.NO_DEVICE,
            detail="aucun appareil inscrit",
        )
        make_device(Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK))
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 2, "mobile": True},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})

    # -- helpers ------------------------------------------------------------------------------------

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def small_targets(self) -> list:
        """The buttons and summaries of <main>, outside a table, drawn and
        under 44 px tall: their text and height."""
        return self.script(
            "var found = [];"
            "document.querySelectorAll('main .btn, main .link-button, main summary').forEach(function (e) {"
            " if (e.closest('td, th') || !e.getClientRects().length) return;"
            " var r = e.getBoundingClientRect();"
            " if (r.height < 44) found.push([e.textContent.trim(), r.height]);"
            "});"
            "return found;"
        )

    def smallest_field_font(self) -> float:
        return self.script(
            "var sizes = Array.from(document.querySelectorAll("
            "'main input:not([type=hidden]):not([type=checkbox]):not([type=radio]), main select, main textarea'))"
            ".filter(function (f) { return f.getClientRects().length; })"
            ".map(function (f) { return parseFloat(getComputedStyle(f).fontSize); });"
            "return sizes.length ? Math.min.apply(null, sizes) : 16;"
        )

    # -- the pages ----------------------------------------------------------------------------------

    def test_the_three_pages_fit_a_phone_with_thumb_sized_targets(self):
        for name in ("notifications:home", "notifications:reminders", "notifications:events"):
            with self.subTest(page=name):
                self.open(reverse(name))
                # Every fold opened: what it hides must fit too.
                self.script("document.querySelectorAll('main details').forEach(function (d) { d.open = true; });")
                self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)
                self.assertEqual(self.small_targets(), [])
                self.assertGreaterEqual(self.smallest_field_font(), 16)

    def test_this_device_shows_one_state_and_never_the_no_javascript_one(self):
        self.open(reverse("notifications:home"))

        def shown():
            return self.script(
                "return Array.from(document.querySelectorAll('#cet-appareil [data-device-state]'))"
                ".filter(function (e) { return !e.hidden; })"
                ".map(function (e) { return e.getAttribute('data-device-state'); });"
            )

        self.wait_for(lambda: "nojs" not in shown() and len(shown()) == 1)
        self.assertNotEqual(shown(), ["nojs"])
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)
