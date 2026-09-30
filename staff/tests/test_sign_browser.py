"""The employee's signing page in a real (headless) Chrome, as a phone:
what static/js/signature_pad.js does, which the test client cannot see.

* **A finger draws, and so does a mouse** (pointer events, touch emulated
  through the DevTools protocol); « Annuler le dernier trait » takes the last
  stroke off, « Effacer » all of them; a form sent with nothing drawn does
  not leave the page and says why.
* **The picture sent is at most 1200 × 400 pixels** whatever the screen's
  density (a 3× phone draws a 343 × 176 frame), transparent around dark ink -
  what `signing.clean_signature_png` keeps.
* **A refused post paints the drawing back**: he does not sign twice because
  a box was left unticked. And « Je signe avec des réserves » shows its text
  box.
* It all runs under the page's Content-Security-Policy, which allows this
  site's own script only.
* **Thumb-sized on a touch screen, small with a mouse**: the pad's two
  buttons at 375 px, and since 30/09 the owner's month page's too (the
  « touch » section of marginmate.css), which a mouse keeps small at any
  width.
* **The employer's pad** (« Contresigner… » on the month's page, 28/09) is
  the same script in a frame folded away: opened, it takes its size, draws
  with a mouse or a finger, refuses to leave with nothing drawn, and paints a
  refused drawing back (`CountersignPadInBrowserTests`).

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Names INVENTED; keys, files and timestamps
offline (signing_support) - the live server runs in this process, so the
patches hold for it.
"""

import io
import tempfile
from datetime import date

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse
from PIL import Image

from invoices.scrapers import website
from staff import private_files, public_views, signature_requests as requests_
from staff.models import Establishment, SignatureRequest
from staff.tests.signing_support import FailingTimestamper, OfflineTimestamps, SigningTestMixin, drawn_signature
from staff.tests.support import employee
from staff.timesheet import save_month
from tests.runner import log_in_the_browser

JUNE = date(2026, 6, 1)

INK = """
    var canvas = document.querySelector('[data-signature-canvas]');
    var data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    var count = 0;
    for (var index = 3; index < data.length; index += 4) if (data[index] > 16) count++;
    return count;
"""


class PadInBrowserCase(SigningTestMixin, StaticLiveServerTestCase):
    """A headless Chrome, and the hand that draws on a pad."""

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
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE")
        self.person = employee()
        save_month(self.person, JUNE, [])
        self.request, self.token = requests_.create_request(self.person, JUNE)

    def device(self, width, height, scale, *, touch):
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": touch},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})

    # -- helpers --------------------------------------------------------------

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

    def click(self, css):
        element = self.element(css)
        self.script("arguments[0].scrollIntoView({block: 'center'})", element)
        element.click()

    def frame(self):
        """The canvas brought to the middle of the screen: its box, in CSS pixels."""
        canvas = self.element("[data-signature-canvas]")
        self.script("arguments[0].scrollIntoView({block: 'center'})", canvas)
        return self.script(
            "var box = arguments[0].getBoundingClientRect();"
            "return {left: box.left, top: box.top, width: box.width, height: box.height};",
            canvas,
        )

    def points(self, shape):
        box = self.frame()
        return [(box["left"] + x * box["width"], box["top"] + y * box["height"]) for x, y in shape]

    def finger(self, shape):
        """A stroke by touch, through the DevTools protocol: what a phone sends."""
        points = self.points(shape)
        send = self.driver.execute_cdp_cmd
        send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": points[0][0], "y": points[0][1]}]})
        for x, y in points[1:]:
            send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
        send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def mouse(self, shape):
        points = self.points(shape)
        send = self.driver.execute_cdp_cmd
        x, y = points[0]
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        for x, y in points[1:]:
            send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1})
        send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    def ink(self) -> int:
        return self.script(INK)

    def tick(self, name):
        box = self.element(f'input[name="{name}"]')
        self.script("arguments[0].scrollIntoView({block: 'center'})", box)
        if not box.is_selected():
            box.click()

    def backing_store(self):
        return self.script(
            "var canvas = document.querySelector('[data-signature-canvas]'); return [canvas.width, canvas.height];"
        )

    ZIGZAG = [(0.1, 0.7), (0.2, 0.3), (0.3, 0.75), (0.4, 0.25), (0.5, 0.7), (0.6, 0.35)]
    LOOP = [(0.65, 0.6), (0.75, 0.3), (0.85, 0.6), (0.9, 0.45)]


@tag("browser")
class SigningPadInBrowserTests(PadInBrowserCase):
    def setUp(self):
        super().setUp()
        # A phone of 375 CSS pixels at 3 device pixels each, with a touch screen.
        self.device(375, 812, 3, touch=True)
        self.open(reverse("staff:sign", args=[self.token]))
        code = requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER)
        self.element("#code-field").send_keys(code)
        self.click(".code-row button")
        self.wait_for(lambda: self.driver.find_elements("css selector", "[data-signature-canvas]"))

    def submit(self):
        self.click(".public-submit")

    # -- the tests --------------------------------------------------------------

    def test_a_finger_and_a_mouse_draw_undo_and_clear_then_it_signs(self):
        width, height = self.backing_store()
        # 176 CSS pixels tall at 3×: capped to the 400 the server accepts.
        self.assertLessEqual(width, 1200)
        self.assertEqual(height, 400)

        # Nothing drawn: the form does not leave, and says why.
        self.tick(public_views.STATEMENT_FIELD)
        self.submit()
        self.assertTrue(self.element("[data-signature-missing]").is_displayed())
        self.assertEqual(SignatureRequest.objects.get().status, SignatureRequest.Status.PENDING)

        self.finger(self.ZIGZAG)
        by_finger = self.ink()
        self.assertGreater(by_finger, 500)
        self.assertFalse(self.element("[data-signature-missing]").is_displayed())
        self.mouse(self.LOOP)
        both = self.ink()
        self.assertGreater(both, by_finger)
        self.click("[data-signature-undo]")
        # The mouse's stroke gone, the finger's drawn again - to a pixel or
        # two of anti-aliasing.
        self.assertAlmostEqual(self.ink(), by_finger, delta=by_finger * 0.02)
        self.assertLess(self.ink(), both - (both - by_finger) // 2)
        self.click("[data-signature-clear]")
        self.assertEqual(self.ink(), 0)

        self.finger(self.ZIGZAG)
        self.submit()
        self.wait_for(lambda: self.driver.find_elements("css selector", "[data-signed]"))
        request = SignatureRequest.objects.get()
        self.assertEqual(request.status, SignatureRequest.Status.EMPLOYEE_SIGNED)
        drawing = Image.open(io.BytesIO(
            private_files.read_checked(request.uuid, private_files.SIGNATURE_IMAGE, request.signature_png_sha256)
        ))
        self.assertEqual(drawing.mode, "RGBA")
        self.assertLessEqual(drawing.width, 1200)
        self.assertLessEqual(drawing.height, 400)
        self.assertEqual(drawing.getpixel((0, 0))[3], 0)          # transparent around the ink

    def test_thumb_sized_controls_and_boxes_beside_their_words(self):
        """At 375 px: « Annuler le dernier trait » and « Effacer » are tapped
        with a thumb - 44 px tall at least, where .btn-small made them 30 -
        and each checkbox is a row, its words beside it down to their last
        line: `.public-form > label { display: block }` outweighed
        `.public-check` and put the certification's second line under the
        box (review, 28/09).

        The owner's pages: since 30/09 every button of `<main>` is 44 px on
        a touch screen too (the « touch » section of marginmate.css, UX
        review - .btn-small was 30 px under a thumb on every page), and
        stays small with a mouse, at any width. Measured on a button that is
        drawn and outside a table's cell (36 px there): the first .btn-small
        of the month's page can sit in the countersign pad folded away,
        where it is 0 px tall and would say nothing."""
        for css in ("[data-signature-undo]", "[data-signature-clear]"):
            with self.subTest(button=css):
                height = self.script("return arguments[0].getBoundingClientRect().height;", self.element(css))
                self.assertGreaterEqual(height, 44)
        labels = self.driver.find_elements("css selector", ".public-check")
        self.assertEqual(len(labels), 2)
        for label in labels:
            box_right, lines, display = self.script(
                "var label = arguments[0], range = document.createRange();"
                "range.selectNodeContents(label.querySelector('span'));"
                "return [label.querySelector('input').getBoundingClientRect().right,"
                " Array.from(range.getClientRects()).map(function (r) { return r.left; }),"
                " getComputedStyle(label).display];",
                label,
            )
            with self.subTest(label=label.text):
                self.assertEqual(display, "flex")
                for left in lines:
                    self.assertGreater(left, box_right)
        # The certification wraps at this width: its second line is the one
        # that fell under the box.
        certification = labels[0]
        line_count = self.script(
            "var range = document.createRange(); range.selectNodeContents(arguments[0].querySelector('span'));"
            "return new Set(Array.from(range.getClientRects()).map(function (r) { return Math.round(r.top); })).size;",
            certification,
        )
        self.assertGreater(line_count, 1)
        # The owner's pages want the owner's login (the employee's page above
        # needs none). On this touch screen their small buttons are thumb-sized
        # too; with a mouse they stay small - in a window as narrow as this
        # phone too: a touch screen is (pointer: coarse), not a width.
        self.assertTrue(self.script("return window.matchMedia('(pointer: coarse)').matches;"))
        log_in_the_browser(self.driver, self.live_server_url)
        month = reverse("staff:month", args=[self.person.pk, JUNE])
        self.open(month)
        self.assertGreaterEqual(self.first_drawn_small_button_height(), 44)
        for width, height in ((375, 812), (1280, 900)):
            with self.subTest(mouse=width):
                self.device(width, height, 2, touch=False)
                self.open(month)
                self.assertFalse(self.script("return window.matchMedia('(pointer: coarse)').matches;"))
                self.assertLess(self.first_drawn_small_button_height(), 44)

    def first_drawn_small_button_height(self) -> float:
        """The height of the month page's first .btn-small that is drawn and
        sits outside a table's cell."""
        height = self.script(
            "var button = Array.from(document.querySelectorAll('main .btn-small')).find(function (e) {"
            " return e.getClientRects().length && !e.closest('td, th'); });"
            "return button ? button.getBoundingClientRect().height : null;"
        )
        self.assertIsNotNone(height, "aucun .btn-small dessiné hors d'un tableau")
        return height

    def test_a_refused_post_paints_the_drawing_back(self):
        box = self.element("[data-reservations]")
        self.assertFalse(box.is_displayed())
        self.finger(self.ZIGZAG)
        drawn = self.ink()
        self.tick(public_views.STATEMENT_FIELD)
        self.tick(public_views.RESERVED_FIELD)
        self.assertTrue(self.element("[data-reservations]").is_displayed())
        self.submit()   # the reservations left empty: refused
        self.wait_for(lambda: public_views.RESERVATION_EMPTY in self.element(".message-error").text)
        self.wait_for(lambda: self.ink() > 0)
        # Painted back at the frame's size: about the same ink, not a new drawing to make.
        self.assertGreater(self.ink(), drawn * 0.8)
        self.element("#reservations-field").send_keys("Le 12, j'ai fini à 23 h.")
        self.submit()
        self.wait_for(lambda: self.driver.find_elements("css selector", "[data-signed]"))
        request = SignatureRequest.objects.get()
        self.assertEqual(request.reservation, "Le 12, j'ai fini à 23 h.")


@tag("browser")
class CountersignPadInBrowserTests(PadInBrowserCase):
    """The employer's pad on the month's page (28/09: « I cannot draw my
    signature as the employer »): the same script, in a frame folded away
    under « Contresigner… » - it must take its size when opened, draw with a
    mouse or a finger, refuse to leave with nothing drawn, and paint a
    refused drawing back."""

    def setUp(self):
        super().setUp()
        session = {}
        code = requests_.issue_code(self.request, SignatureRequest.Identification.CODE_HANDED_OVER)
        requests_.check_code(self.request, code, session)
        requests_.sign_for_employee(self.request, drawn_signature(), session=session, statement_accepted=True)
        # The month's page is the owner's, behind the login (the employee's
        # pad above needs none).
        log_in_the_browser(self.driver, self.live_server_url)

    def month_page(self):
        self.open(reverse("staff:month", args=[self.person.pk, JUNE]))

    def open_pad(self):
        self.click(".signature-countersign > summary")
        self.wait_for(lambda: self.backing_store()[0] > 10)

    def countersign(self):
        self.click(".signature-countersign-form button[type=submit]")

    def section(self) -> str:
        """The « Signature » section's text, read afresh: an element held
        across the page's navigation goes stale."""
        return self.script(
            "var section = document.querySelector('#signature'); return section ? section.innerText : '';"
        )

    def countersigned(self):
        self.wait_for(lambda: "Signée et contresignée" in self.section())

    def test_opened_it_draws_undoes_clears_refuses_nothing_then_countersigns(self):
        self.device(1280, 900, 2, touch=False)
        self.month_page()
        # Folded away until « Contresigner… » is clicked.
        self.assertFalse(self.element("[data-signature-canvas]").is_displayed())
        self.open_pad()
        width, height = self.backing_store()
        self.assertLessEqual(width, 1200)
        self.assertLessEqual(height, 400)

        # Nothing drawn: the page does not leave, and says why.
        self.countersign()
        self.assertTrue(self.element("[data-signature-missing]").is_displayed())
        self.assertEqual(SignatureRequest.objects.get().status, SignatureRequest.Status.EMPLOYEE_SIGNED)

        self.mouse(self.ZIGZAG)
        first = self.ink()
        self.assertGreater(first, 300)
        self.mouse(self.LOOP)
        both = self.ink()
        self.assertGreater(both, first)
        self.click("[data-signature-undo]")
        self.assertAlmostEqual(self.ink(), first, delta=first * 0.02)
        self.click("[data-signature-clear]")
        self.assertEqual(self.ink(), 0)

        self.mouse(self.ZIGZAG)
        self.countersign()
        self.countersigned()
        request = SignatureRequest.objects.get()
        self.assertEqual(request.status, SignatureRequest.Status.COMPLETE)
        digest = requests_.employer_drawing_sha256(request)
        drawing = Image.open(io.BytesIO(
            private_files.read_checked(request.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE, digest)
        ))
        self.assertEqual(drawing.mode, "RGBA")
        self.assertLessEqual((drawing.width, drawing.height), (1200, 400))
        self.assertEqual(drawing.getpixel((0, 0))[3], 0)
        self.assertIn("Signature dessinée de l'employeur (PNG)", self.section())

    def test_a_finger_draws_at_375_px_and_a_refused_drawing_is_painted_back(self):
        """On a phone, with no timestamp server answering: refused beside the
        pad, the pad open again and the drawing on it - countersigned at the
        next try without drawing again."""
        self.device(375, 812, 3, touch=True)
        self.month_page()
        self.open_pad()
        self.assertEqual(self.backing_store()[1], 400)   # 176 CSS px at 3×, capped
        for css in ("[data-signature-undo]", "[data-signature-clear]"):
            height = self.script("return arguments[0].getBoundingClientRect().height;", self.element(css))
            self.assertGreaterEqual(height, 44)
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), 375)
        self.finger(self.ZIGZAG)
        drawn = self.ink()
        self.assertGreater(drawn, 300)
        with OfflineTimestamps(stampers=[FailingTimestamper()]):
            self.countersign()
            self.wait_for(lambda: self.driver.find_elements("css selector", "[data-countersign-error]"))
        self.assertIn("horodatage", self.element("[data-countersign-error]").text)
        self.wait_for(lambda: self.ink() > 0)
        self.assertGreater(self.ink(), drawn * 0.8)
        self.countersign()
        self.countersigned()
        self.assertEqual(SignatureRequest.objects.get().status, SignatureRequest.Status.COMPLETE)
