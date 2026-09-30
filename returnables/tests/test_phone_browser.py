"""/consignes/ in a real (headless) Chrome, as a 375 × 667 phone: what
static/js/returnables.js and the page's CSS do, which the test client cannot
see.

* **It fits the phone**: no sideways scroll, « Prendre une photo » in the
  first screen, the kegs' + a thumb's size (44 px at least - 56 here), every
  field 16 px at least (iOS zooms the page in on a smaller one), and the
  topbar scrolling away with the page.
* **The steppers**: two quick taps on + make 2, the page never zooms, nothing
  is sent; − stops at 0, + at 9 999.
* **The photos**: a shot is kept in a hidden input and a fresh one takes its
  place, with a preview and « Retirer »; the eleventh is refused on the page;
  what is kept is what the server saves.
* **The keypad's key**: Enter in a count (Android's « next » / « done »)
  moves to the next count, closes the keypad on the last, and never sends
  the reprise half counted.
* **The draft**: a count typed and the tab lost comes back, keyed by the
  espace, and is forgotten once the reprise is saved; a note restored is
  said and shown; « Effacer » puts back today and « Repris par ».
* **The stale tab**: shown again the next day, the date moves to today.
* **Thumb-sized**: every small button, « Effacer » and a photo's « Retirer »
  44 px tall at least; a long file name never scrolls the page sideways; an
  upload's result is in the screen its redirect lands on.
* **« Menu »** (30/09, the topbar folded under 860 px): it opens under the
  bar, which scrolls away with the page here; a tap on the veil around it
  only shuts it - never a count field focused under the finger - and a
  finger dragged on the veil scrolls nothing (the bar would carry its menu
  off the screen).

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented; photos of a
few pixels.
"""

import json
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse
from django.utils import timezone

from invoices.scrapers import website
from returnables.models import Pickup, PickupPhoto, Slip
from returnables.tests.support import DELIVERY_DAY, seeded_type, tiny_jpeg, tiny_pdf
from tests.runner import log_in_the_browser

WIDTH, HEIGHT = 375, 667


def draft_key() -> str:
    """The reprise draft's key in the test espace: returnables.js builds it
    from the espace's scope (base.html's <body data-tenant>,
    accounts.tenancy.storage_scope - never the espace's id since 29/09)."""
    from accounts.tenancy import storage_scope
    from tests.runner import TEST_TENANT

    return f"marginmate:espace-{storage_scope(TEST_TENANT)}:consignes:brouillon"


#: A file name as a phone or a driver's mail names it: no space to break at.
LONG_NAME = "Bon_de_livraison_EXEMPLE_20310312_numero_000123_exemplaire_A.pdf"


@tag("browser")
class ConsignesOnAPhoneInBrowserTests(StaticLiveServerTestCase):
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
        cls.photos_dir = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 2, "mobile": True}
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})
        self.open("/consignes/")
        self.script("localStorage.clear();")
        self.open("/consignes/")
        self.kegs = f"nombre-{seeded_type('Fûts').pk}"

    # -- helpers ------------------------------------------------------------------------------------

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def element(self, css):
        return self.driver.find_element("css selector", css)

    def box(self, css):
        return self.script(
            "var r = document.querySelector(arguments[0]).getBoundingClientRect();"
            "return {left: r.left, top: r.top, width: r.width, height: r.height};",
            css,
        )

    def tap(self, css):
        """A finger's tap in the middle of the element, as the phone sends it."""
        element = self.element(css)
        self.script("arguments[0].scrollIntoView({block: 'center'})", element)
        box = self.box(css)
        self.touch(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)

    def touch(self, x, y):
        """A finger's tap at (x, y), whatever is there."""
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def drag(self, x, y, to_y, steps=12):
        """A finger put down at (x, y), dragged to (x, to_y) and held there
        before it lifts: no fling goes on scrolling after it (a fling still
        running takes the next tap to stop itself)."""
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for step in range(1, steps + 1):
            at = y + (to_y - y) * step / steps
            self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": at}]})
        time.sleep(0.3)
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": to_y}]})
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def scrolled_still(self):
        """Until the page stops scrolling."""
        seen = []

        def still():
            seen.append(self.script("return window.scrollY;"))
            time.sleep(0.1)
            return len(seen) > 2 and seen[-1] == seen[-2] == seen[-3]

        self.wait_for(still)
        return seen[-1]

    def menu_state(self):
        """« Menu »'s aria-expanded - None where there is no « Menu »."""
        return self.script(
            "var toggle = document.querySelector('[data-topbar-toggle]');"
            "return toggle ? toggle.getAttribute('aria-expanded') : null;"
        )

    def under(self, x, y):
        """What a finger at (x, y) lands on: a field's name, else its class."""
        return self.script(
            "var e = document.elementFromPoint(arguments[0], arguments[1]);"
            "return e ? (e.getAttribute('name') || e.className) : null;",
            x, y,
        )

    def open_the_menu(self):
        """« Menu » tapped, the page at its top - where the bar is, here."""
        self.assertEqual(self.script("return window.scrollY;"), 0)
        self.assertEqual(self.menu_state(), "false", "« Menu » drawn shut")
        box = self.box("[data-topbar-toggle]")
        self.touch(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)
        self.wait_for(lambda: self.menu_state() == "true")

    def value(self, name):
        return self.script("return document.querySelector('[name=\"' + arguments[0] + '\"]').value;", name)

    def jpeg(self, name):
        path = self.photos_dir / name
        path.write_bytes(tiny_jpeg())
        return str(path)

    def heights(self, css):
        """{text: height} of the elements matching `css` that are drawn."""
        return self.script(
            "var found = {};"
            "document.querySelectorAll(arguments[0]).forEach(function (e) {"
            " if (!e.getClientRects().length) return;"
            " found[e.textContent.trim() || e.tagName] = e.getBoundingClientRect().height; });"
            "return found;",
            css,
        )

    def draft(self, **fields):
        """A draft left in the browser, as returnables.js writes one."""
        stored = {"saved_at": None, "date": "", "supplier": "", "counts": {}, "note": "", "photos": 0, **fields}
        self.script(
            "var draft = JSON.parse(arguments[1]); draft.saved_at = Date.now();"
            "localStorage.setItem(arguments[0], JSON.stringify(draft));",
            draft_key(),
            json.dumps(stored),
        )

    def today(self) -> str:
        return self.script("return document.querySelector('input[data-today]').getAttribute('data-today');")

    def offered_supplier(self) -> str:
        return self.script(
            "var s = document.querySelector('select[name=supplier]');"
            "var o = Array.from(s.options).filter(function (o) { return o.defaultSelected; })[0];"
            "return o ? o.value : '';"
        )

    # -- the page on a phone -----------------------------------------------------------------------

    def test_the_page_fits_a_phone(self):
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)
        self.assertLess(self.box("[data-photo-slot]")["top"], 600)
        plus = ".count-row.is-main .stepper-btn[data-step='1']"
        self.assertTrue(self.script("return !document.querySelector(arguments[0]).hidden;", plus))
        size = self.box(plus)
        self.assertGreaterEqual(min(size["width"], size["height"]), 56)
        for other in self.script(
            "return Array.from(document.querySelectorAll('.stepper-btn')).map(function (b) {"
            " var r = b.getBoundingClientRect(); return Math.min(r.width, r.height); });"
        ):
            self.assertGreaterEqual(other, 44)
        smallest = self.script(
            "return Math.min.apply(null, Array.from(document.querySelectorAll("
            "'.consignes-page input:not([type=hidden]), .consignes-page select, .consignes-page textarea'))"
            ".map(function (f) { return parseFloat(getComputedStyle(f).fontSize); }));"
        )
        self.assertGreaterEqual(smallest, 16)
        # It scrolls away with the page (not sticky), and stays the box its
        # menu and veil are drawn in: relative, not static (30/09) - static,
        # its z-index went and the veil dropped behind the page.
        self.assertEqual(self.script("return getComputedStyle(document.querySelector('.topbar')).position;"), "relative")
        # And the page leaves it no room above what it scrolls to: the bar is
        # not there once scrolled. html:has(.consignes-page) weighs what the
        # folded bar's html.topbar-menu-ready does - coming later is what
        # makes 0 win over 6rem, here only.
        self.assertTrue(self.script("return document.documentElement.classList.contains('topbar-menu-ready');"))
        self.assertEqual(self.script("return getComputedStyle(document.documentElement).scrollPaddingTop;"), "0px")
        # The draft of nothing is nothing: no notice on a fresh page.
        self.assertTrue(self.script("return document.querySelector('[data-draft-notice]').hidden;"))

    def test_the_menu_opens_under_the_bar_and_a_tap_on_the_veil_types_nothing(self):
        """The bar scrolls away with the page here, and « Menu » still opens
        under it, over the counts. A tap beside the menu - on its veil, over
        a count - only shuts it: on a phone the tap that closes a menu would
        land on the field under the finger, and the keypad come up over a
        count nobody meant to type. A taller phone (812 px), so that counts
        lie under the veil below the open menu."""
        tall = 812
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": tall, "deviceScaleFactor": 2, "mobile": True}
        )
        self.open("/consignes/")
        self.script("window.pageMark = 'toujours là';")
        self.open_the_menu()
        menu, bar = self.box("#topbar-menu"), self.box(".topbar")
        self.assertLessEqual(abs(menu["top"] - (bar["top"] + bar["height"])), 1, (menu, bar))
        self.assertLessEqual(menu["left"] + menu["width"], WIDTH)
        bottom = menu["top"] + menu["height"]
        counts = self.script(
            "return Array.from(document.querySelectorAll('input[name^=\"nombre-\"]')).map(function (f) {"
            " var r = f.getBoundingClientRect(); return [f.name, r.left + r.width / 2, r.top + r.height / 2]; });"
        )
        below = [count for count in counts if bottom + 8 < count[2] < tall - 4]
        self.assertTrue(below, f"no count under the veil: menu to {bottom:.0f}, counts {counts}")
        name, x, y = below[0]
        self.assertEqual(self.under(x, y), "topbar", "the veil is over the count")
        self.touch(x, y)
        self.wait_for(lambda: self.menu_state() == "false")
        time.sleep(0.3)   # a focus, a keypad or a page left would be there by now
        self.assertFalse(self.script("return document.activeElement.matches('input, select, textarea');"))
        self.assertEqual(self.value(name), "")
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        self.assertEqual(self.script("return location.pathname + location.search;"), "/consignes/")
        self.assertEqual(self.script("return window.visualViewport.scale;"), 1)
        # Shut, the veil is gone: the count is what a tap there reaches.
        self.assertEqual(self.under(x, y), name)

    def test_a_finger_dragged_on_the_veil_scrolls_nothing(self):
        """Here the bar is not sticky: a page scrolled under the open menu
        carried the bar and its menu off the top of the screen, the veil
        still dimming all of it, nothing left in sight to close it with. The
        veil takes no scrolling (touch-action: none). The same drag, the menu
        shut, scrolls the page - what the veil has to stop."""
        x, y, to_y = 20, HEIGHT - 20, HEIGHT - 260
        self.assertGreater(self.script("return document.documentElement.scrollHeight;"), HEIGHT + 300)
        self.drag(x, y, to_y)
        self.assertGreater(self.scrolled_still(), 100)
        self.script("window.scrollTo(0, 0);")
        self.assertEqual(self.scrolled_still(), 0)

        self.open_the_menu()
        menu = self.box("#topbar-menu")
        self.assertLess(menu["top"] + menu["height"], y - 8, "the finger is on the veil, below the menu")
        self.assertEqual(self.under(x, y), "topbar")
        self.drag(x, y, to_y)
        self.assertEqual(self.scrolled_still(), 0, "a finger dragged on the veil scrolled the page under the open menu")
        bar = self.box(".topbar")
        self.assertEqual(bar["top"], 0)
        self.assertLessEqual(abs(self.box("#topbar-menu")["top"] - menu["top"]), 2)

    def test_two_quick_taps_on_plus_make_two_and_nothing_is_sent(self):
        self.script("window.pageMark = 'toujours là';")
        plus = ".count-row.is-main .stepper-btn[data-step='1']"
        self.tap(plus)
        time.sleep(0.1)
        self.tap(plus)
        self.wait_for(lambda: self.value(self.kegs) == "2")
        self.assertEqual(self.script("return window.visualViewport.scale;"), 1)
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        self.assertEqual(self.script("return location.pathname + location.search;"), "/consignes/")
        self.assertFalse(Pickup.objects.exists())

    def test_minus_stops_at_zero_and_plus_at_9999(self):
        minus = ".count-row.is-main .stepper-btn[data-step='-1']"
        plus = ".count-row.is-main .stepper-btn[data-step='1']"
        self.element(minus).click()
        self.assertEqual(self.value(self.kegs), "0")
        self.script("document.querySelector('[name=\"' + arguments[0] + '\"]').value = '9999';", self.kegs)
        self.element(plus).click()
        self.assertEqual(self.value(self.kegs), "9999")

    def test_the_keypad_s_key_moves_to_the_next_count_and_never_sends_the_reprise(self):
        """Android's keypad key is an Enter: in a field it sent the form,
        the reprise saved with the kegs typed and nothing else."""
        from selenium.webdriver.common.keys import Keys

        self.script("window.pageMark = 'toujours là';")
        crates = f"nombre-{seeded_type('Caisses verre').pk}"
        co2 = f"nombre-{seeded_type('Bouteilles CO2').pk}"
        field = self.element(f"[name='{self.kegs}']")
        field.send_keys("15")
        field.send_keys(Keys.ENTER)
        time.sleep(0.5)   # a post, had one been sent, would have landed by now
        self.assertFalse(Pickup.objects.exists())
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        self.wait_for(lambda: self.script("return document.activeElement.name;") == crates)
        self.element(f"[name='{crates}']").send_keys("2", Keys.ENTER)
        self.wait_for(lambda: self.script("return document.activeElement.name;") == co2)
        self.element(f"[name='{co2}']").send_keys("1", Keys.ENTER)
        # The last count's key closes the keypad: nothing focused any more.
        self.wait_for(lambda: self.script("return document.activeElement === document.body;"))
        time.sleep(0.5)   # a post, had one been sent, would have landed by now
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        self.assertEqual(self.script("return location.pathname + location.search;"), "/consignes/")
        self.assertEqual((self.value(self.kegs), self.value(crates), self.value(co2)), ("15", "2", "1"))
        self.assertFalse(Pickup.objects.exists())

    # -- the photos -----------------------------------------------------------------------------------

    def test_a_shot_is_kept_a_fresh_input_takes_its_place_and_the_server_saves_it(self):
        capture = self.element("input[data-photo-capture]")
        capture.send_keys(self.jpeg("IMG_0001.jpg"))
        self.wait_for(lambda: self.script("return document.querySelectorAll('[data-photo-previews] li').length;") == 1)
        self.assertEqual(self.script("return document.querySelectorAll('[data-photo-inputs] input').length;"), 1)
        fresh = self.script("return document.querySelector('[data-photo-slot] input[data-photo-capture]').files.length;")
        self.assertEqual(fresh, 0)
        self.element("input[data-photo-capture]").send_keys(self.jpeg("IMG_0002.jpg"))
        self.wait_for(lambda: self.script("return document.querySelectorAll('[data-photo-previews] li').length;") == 2)
        # « Retirer » the first: its preview and its input go.
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.assertEqual(self.script("return document.querySelectorAll('[data-photo-previews] li').length;"), 1)
        self.assertEqual(self.script("return document.querySelectorAll('[data-photo-inputs] input').length;"), 1)
        self.script("document.querySelector('.reprise-submit').click();")
        self.wait_for(lambda: "enregistree" not in self.script("return location.search;") and PickupPhoto.objects.exists())
        self.assertEqual(PickupPhoto.objects.count(), 1)
        self.assertEqual(Pickup.objects.count(), 1)

    def test_the_eleventh_photo_is_refused_on_the_page(self):
        gallery = self.element("input[data-photo-gallery]")
        gallery.send_keys("\n".join(self.jpeg(f"IMG_{number:04d}.jpg") for number in range(11)))
        self.wait_for(lambda: self.script("return document.querySelectorAll('[data-photo-previews] li').length;") == 10)
        said = self.script("return document.querySelector('[data-photo-refused]').textContent;")
        self.assertEqual(said, "10 photos au plus : 1 photo n'a pas été ajoutée.")

    # -- the draft, the stale tab ----------------------------------------------------------------------

    def test_a_count_typed_comes_back_and_is_forgotten_once_saved(self):
        field = self.element(f"[name='{self.kegs}']")
        field.send_keys("15")
        key = draft_key()
        self.assertIn('"15"', self.script("return localStorage.getItem(arguments[0]);", key))
        self.open("/consignes/")
        self.assertEqual(self.value(self.kegs), "15")
        notice = self.script("return document.querySelector('[data-draft-notice]').textContent;")
        self.assertEqual(notice, "Comptage non envoyé retrouvé (Fûts 15) — Effacer")
        self.script("document.querySelector('.reprise-submit').click();")
        self.wait_for(lambda: Pickup.objects.exists() and self.script("return document.readyState") == "complete"
                      and self.script("return location.search;") == "")
        self.assertIsNone(self.script("return localStorage.getItem(arguments[0]);", key))
        self.assertEqual(self.value(self.kegs), "")

    def test_effacer_forgets_the_draft(self):
        self.element(f"[name='{self.kegs}']").send_keys("7")
        self.assertIsNotNone(self.script("return localStorage.getItem(arguments[0]);", draft_key()))
        self.open("/consignes/")
        self.script("document.querySelector('[data-draft-notice] button').click();")
        self.assertEqual(self.value(self.kegs), "")
        self.assertIsNone(self.script("return localStorage.getItem(arguments[0]);", draft_key()))

    def test_effacer_puts_back_today_and_repris_par(self):
        """A count typed yesterday evening, offered this morning, sets the
        day and « Repris par » to the draft's: « Effacer » puts back what the
        page offers - else today's reprise was saved dated yesterday."""
        today = self.today()
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        offered = self.offered_supplier()
        self.assertNotEqual(offered, "")
        self.draft(date=yesterday, supplier="", counts={self.kegs: "7"})
        self.open("/consignes/")
        self.assertEqual(self.value("date"), yesterday)
        self.assertEqual(self.value("supplier"), "")
        self.script("document.querySelector('[data-draft-notice] button').click();")
        self.assertEqual(self.value(self.kegs), "")
        self.assertEqual(self.value("date"), today)
        self.assertEqual(self.value("supplier"), offered)
        self.assertEqual(
            self.script("return document.querySelector('[data-summary-date]').textContent;"),
            "/".join(reversed(today.split("-"))),
        )
        self.assertEqual(self.script("return document.querySelector('[data-summary-supplier]').textContent;"), "UBA")
        self.assertIsNone(self.script("return localStorage.getItem(arguments[0]);", draft_key()))

    def test_a_note_restored_is_said_and_shown(self):
        """The note sits in the folded part: restored unseen, yesterday's
        note was saved with the next reprise."""
        self.draft(date=self.today(), supplier=self.offered_supplier(), note="fût abîmé")
        self.open("/consignes/")
        notice = self.script("return document.querySelector('[data-draft-notice]').textContent;")
        self.assertEqual(notice, "Comptage non envoyé retrouvé (une note) — Effacer")
        self.assertTrue(self.script("return document.querySelector('.reprise-details').open;"))
        self.assertEqual(self.value("note"), "fût abîmé")
        # A draft whose counts' types left the form says nothing in brackets.
        self.draft(date=self.today(), counts={"nombre-999999": "4"})
        self.open("/consignes/")
        notice = self.script("return document.querySelector('[data-draft-notice]').textContent;")
        self.assertEqual(notice, "Comptage non envoyé retrouvé — Effacer")

    def test_a_tab_shown_again_the_next_day_moves_to_today(self):
        today = self.script("return document.querySelector('input[data-today]').getAttribute('data-today');")
        self.script(
            "var field = document.querySelector('input[data-today]');"
            "field.setAttribute('data-today', '2000-01-02'); field.value = '2000-01-02'; field.max = '2000-01-02';"
            "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));"
        )
        self.assertEqual(self.script("return document.querySelector('input[data-today]').value;"), today)
        self.assertEqual(self.script("return document.querySelector('input[data-today]').max;"), today)
        day = "/".join(reversed(today.split("-")))
        self.assertEqual(self.script("return document.querySelector('[data-summary-date]').textContent;"), day)

    def test_the_reprise_s_page_fits_a_phone_too(self):
        from returnables.tests.support import make_pickup, make_slip

        pickup = make_pickup(photos=2)
        make_slip()
        self.open(reverse("returnables:pickup_detail", args=[pickup.pk]))
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)
        self.open("/consignes/")
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)

    # -- thumb-sized, never sideways ----------------------------------------------------------------

    def test_every_small_button_is_thumb_sized(self):
        """44 px at least (the spec, CLAUDE.md): .btn-small made « Retirer »,
        « Mettre la reprise au … » and « Dater la reprise du … » 30 px, and
        « Effacer » was a line of text - the saved photo's « Retirer »
        deleting it at once."""
        from returnables.tests.support import make_pickup, make_slip

        # The home page: « Effacer » of a draft, « Retirer » of a preview.
        self.draft(date=self.today(), counts={self.kegs: "3"})
        self.open("/consignes/")
        self.element("input[data-photo-capture]").send_keys(self.jpeg("IMG_0001.jpg"))
        self.wait_for(lambda: self.script("return document.querySelectorAll('[data-photo-previews] li').length;") == 1)
        for css in ("[data-draft-notice] button", "[data-photo-previews] button"):
            with self.subTest(css=css):
                found = self.heights(css)
                self.assertTrue(found)
                self.assertTrue(all(height >= 44 for height in found.values()), found)

        # A reprise's page: its photos (taken the day before), a bon of the
        # next day - « Retirer… », « Dater la reprise du … », « Mettre la
        # reprise au … ».
        evening_before = timezone.make_aware(datetime.combine(DELIVERY_DAY - timedelta(days=1), datetime.min.time()))
        pickup = make_pickup(photos=2, photo_taken_at=evening_before + timedelta(hours=19))
        make_slip(delivery_date=DELIVERY_DAY + timedelta(days=1))
        self.open(reverse("returnables:pickup_detail", args=[pickup.pk]))
        summaries = self.heights(".photo-remove > summary")
        self.assertEqual(len(self.script("return document.querySelectorAll('.photo-remove');")), 2)
        self.assertTrue(all(height >= 44 for height in summaries.values()), summaries)
        self.script("document.querySelectorAll('.photo-remove').forEach(function (d) { d.open = true; });")
        small = self.heights(".consignes-page .btn-small")
        for label in ("Retirer", "Dater la reprise du 09/02/2026", "Mettre la reprise au 11/02/2026"):
            with self.subTest(button=label):
                self.assertIn(label, small)
                self.assertGreaterEqual(small[label], 44)
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)

    def test_a_long_file_name_never_scrolls_the_page_sideways(self):
        """A file name has no space to break at: printed in a bon's subtitle
        or in the upload's message, it widened the page past the phone."""
        from returnables.tests.support import make_slip

        slip = make_slip()
        Slip.objects.filter(pk=slip.pk).update(original_name=LONG_NAME)
        self.open(reverse("returnables:slip_detail", args=[slip.pk]))
        self.assertIn(LONG_NAME, self.script("return document.querySelector('.page-subtitle').textContent;"))
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)

        # The upload refusing it names it - in the first screen of where
        # its redirect lands (#bons), never above the fold.
        document = self.photos_dir / LONG_NAME
        document.write_bytes(tiny_pdf(["FACTURE EXEMPLE", "TOTAL 12.00"]))
        self.open("/consignes/")
        self.element("#bons-fichiers").send_keys(str(document))
        self.script("document.querySelector('#bons form button[type=submit]').click();")
        self.wait_for(
            lambda: self.script("return location.hash;") == "#bons"
            and self.script("return document.readyState;") == "complete"
            and self.script("return !!document.querySelector('#bons .message');")
        )
        said = self.script("return document.querySelector('#bons .message').textContent;")
        self.assertIn(f"{LONG_NAME} : Aucun format de bon ne reconnaît ce document.", said)
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), WIDTH)
        top = self.box("#bons .message")["top"]
        self.assertGreaterEqual(top, 0)
        self.assertLess(top, HEIGHT)
