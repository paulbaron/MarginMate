"""Achats' « Prendre une photo » in a real (headless) Chrome: what
static/js/photos.js, static/js/receipt_camera.js and marginmate.css do with the
import card's camera, which the test client cannot see (the owner, 01/10:
« prendre les factures en photo directement depuis le site, comme pour les
consignes »).

* **On a phone** (375 × 667, a touch screen emulated): the camera's tile is
  drawn, first among the choices, and the page never scrolls sideways.
* **A shot is kept under a name of its own**: iOS names every camera shot
  « image.jpg » - each is renamed photo-YYYYMMDD-HHMMSS.jpg from the moment
  it was taken (its file's date, in the browser's local time), a second of
  the same moment takes « -2 »; a preview per shot, a fresh empty camera
  input under the label, the shot itself in the form's hidden box under its
  new name; « Retirer » takes one back.
* **What Cloudflare would refuse is refused on the page**: with
  `common.ONLINE_SEND_MAX_BYTES` patched to hold exactly one tiny photo, the
  second shot is refused with its sentence - the cap said as the server says
  a weight (`common.weight`, whose twin photos.js gives out is compared with
  it) - and no preview is added. The preview and the refusal are seen with
  the tile: right under it, above the two other choices.
* **Whatever the order**: with a shot waiting, a pick in « Des fichiers »
  that would take the post past the cap is emptied and said (one that fits
  is kept, and the sentence goes), and a selection that changed with no
  "change" event is held at « Importer »: nothing sent, the button as it
  was, the shot kept.
* **A shot is not lost without a word**: leaving the page with one waiting
  cancels « beforeunload » (the browser asks), and a page reloaded with
  shots gone says how many, once.
* **« Importer » posts the shots**: a ReceiptBatch whose pending files carry
  the new names (`receipt_batches.start_batch` is patched: no OCR runs), and
  the button's busy label says the photos are going up while one waits.
* **On a desktop** (1280 × 800, a mouse - the window pinned: headless
  Chrome's own 800 × 600 is a phone's width here): no camera tile, the two
  other choices drawn - and with no shot waiting, « Des fichiers » posts
  past the cap: it guards the shots alone.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented; photos of a
few pixels.
"""

import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts import paths
from common import MEGABYTE, weight
from invoices.models import ReceiptBatch
from invoices.scrapers import website
from returnables.tests.support import tiny_jpeg
from tests.runner import log_in_the_browser

PHONE = (375, 667)
DESKTOP = (1280, 800)

#: A shot renamed: the moment, then the extension (and « -2 »… for a second
#: shot of the same second).
RENAMED = re.compile(r"photo-\d{8}-\d{6}(-\d+)?\.jpg")

#: When the invented shots were taken: their files' modification time.
TAKEN = datetime(2026, 3, 14, 9, 26, 53).timestamp()

PHOTOS_BUSY = "Envoi des photos… gardez la page ouverte"

SLOT = "[data-photo-slot] input[data-photo-capture]"
#: « Des fichiers »: a native input, no photo slot.
FILES = "form[data-receipt-upload] .upload-choices input[multiple]:not([webkitdirectory])"
PREVIEW_NAMES = (
    "return Array.from(document.querySelectorAll('[data-photo-previews] li span'))"
    ".map(function (span) { return span.textContent; });"
)
STORED_NAMES = (
    "return Array.from(document.querySelectorAll('[data-photo-inputs] input')).map(function (input) {"
    " return Array.from(input.files).map(function (file) { return file.name; }); });"
)


class CameraBrowserTestCase(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001 - any failure to start Chrome is a skip, said
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")
        cls.photos_dir = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        photos_dir = getattr(cls, "photos_dir", None)
        if photos_dir is not None:
            shutil.rmtree(photos_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # No OCR thread: the import is staged, never read.
        started = mock.patch("invoices.receipt_batches.start_batch")
        self.started = started.start()
        self.addCleanup(started.stop)
        self.addCleanup(shutil.rmtree, os.path.join(paths.imports_dir(), "receipt_batches"), True)
        log_in_the_browser(self.driver, self.live_server_url)
        self.url = reverse("invoices:invoice_list")

    # -- helpers ------------------------------------------------------------------------------------

    def device(self, width, height, *, touch):
        """A phone (mobile, a touch screen) or a desktop (a mouse)."""
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": touch},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def open_the_card(self):
        """Achats with the browser's memory of the card emptied: « Tickets et
        factures » is then the panel shown."""
        self.open(self.url)
        self.script("localStorage.clear(); sessionStorage.clear();")
        self.open(self.url)

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def drawn(self, css):
        return self.script(
            "var e = document.querySelector(arguments[0]);"
            "return !!e && e.getClientRects().length > 0 && getComputedStyle(e).display !== 'none';",
            css,
        )

    def rects(self, *css):
        """Where each first match sits in the window, in CSS pixels."""
        return self.script(
            "return arguments[0].map(function (css) {"
            " var r = document.querySelector(css).getBoundingClientRect();"
            " return {top: r.top, bottom: r.bottom}; });",
            list(css),
        )

    def document(self, name, data):
        """A file to pick in « Des fichiers », of a few bytes."""
        path = Path(tempfile.mkdtemp(dir=self.photos_dir)) / name
        path.write_bytes(data)
        return str(path)

    def refusal(self):
        return self.script(
            "var p = document.querySelector('[data-photo-refused]'); return p.hidden ? null : p.textContent;"
        )


@tag("browser")
class CameraOnAPhoneInBrowserTests(CameraBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.device(*PHONE, touch=True)
        self.open_the_card()

    def photo(self, folder, when=TAKEN):
        """A shot as iOS names it - « image.jpg » - taken at `when`."""
        path = self.photos_dir / folder / "image.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tiny_jpeg())
        os.utime(path, (when, when))
        return str(path)

    def shoot(self, folder, when=TAKEN):
        before = len(self.script(PREVIEW_NAMES))
        self.driver.find_element("css selector", SLOT).send_keys(self.photo(folder, when))
        self.wait_for(
            lambda: (
                len(self.script(PREVIEW_NAMES)) > before
                or not self.script("return document.querySelector('[data-photo-refused]').hidden;")
            )
        )

    def stamp(self, when=TAKEN) -> str:
        """The moment as the browser's own clock reads it, local time."""
        parts = self.script(
            "var d = new Date(arguments[0]);"
            "return [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds()];",
            int(when * 1000),
        )
        return "photo-{:04d}{:02d}{:02d}-{:02d}{:02d}{:02d}".format(*parts)

    def busy_label(self):
        return self.script(
            "return document.querySelector('form[data-receipt-upload] button[type=submit]').getAttribute('data-busy-label');"
        )

    # -- the tile ---------------------------------------------------------------------------------------

    def test_the_camera_tile_is_drawn_first_and_the_page_fits(self):
        self.assertTrue(self.script("return window.matchMedia('(pointer: coarse)').matches;"), "pas d'écran tactile")
        self.assertTrue(self.drawn(".upload-choice-camera"))
        choices = self.script(
            "return Array.from(document.querySelectorAll('[data-photos] .upload-choice')).map(function (e) {"
            " var r = e.getBoundingClientRect();"
            " return {camera: e.classList.contains('upload-choice-camera'), top: r.top, height: r.height,"
            " drawn: e.getClientRects().length > 0}; });"
        )
        self.assertEqual([choice["camera"] for choice in choices], [True, False, False])
        self.assertTrue(all(choice["drawn"] for choice in choices))
        self.assertLess(choices[0]["top"], choices[1]["top"])
        self.assertGreaterEqual(choices[0]["height"], 44)
        self.assertIn(
            "Prendre une photo", self.script("return document.querySelector('.upload-choice-camera').textContent;")
        )
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), PHONE[0])

    # -- the shots ----------------------------------------------------------------------------------------

    def test_a_shot_is_renamed_kept_and_a_fresh_input_takes_its_place(self):
        expected = self.stamp() + ".jpg"
        self.shoot("premiere")
        self.assertEqual(self.script(PREVIEW_NAMES), [expected])
        self.assertRegex(expected, r"^photo-\d{8}-\d{6}\.jpg$")
        self.assertIsNone(self.refusal())
        # The filled input waits in the hidden box, under the new name; a
        # fresh one, empty, is under the label.
        self.assertEqual(self.script(STORED_NAMES), [[expected]])
        self.assertEqual(self.script(f"return document.querySelectorAll('{SLOT}').length;"), 1)
        self.assertEqual(self.script(f"return document.querySelector('{SLOT}').files.length;"), 0)
        # A second shot of the same second: another name, never the same.
        self.shoot("seconde")
        self.assertEqual(self.script(PREVIEW_NAMES), [expected, self.stamp() + "-2.jpg"])
        self.assertEqual(self.script(STORED_NAMES), [[expected], [self.stamp() + "-2.jpg"]])
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), PHONE[0])
        # « Retirer » the first: its preview and its input go.
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.assertEqual(self.script(PREVIEW_NAMES), [self.stamp() + "-2.jpg"])
        self.assertEqual(self.script(STORED_NAMES), [[self.stamp() + "-2.jpg"]])
        height = self.script(
            "return document.querySelector('[data-photo-previews] li button').getBoundingClientRect().height;"
        )
        self.assertGreaterEqual(height, 44)

    def test_a_shot_past_what_one_post_may_carry_is_refused_and_said(self):
        cap = len(tiny_jpeg())
        with mock.patch("common.ONLINE_SEND_MAX_BYTES", cap):
            self.open_the_card()
        self.assertEqual(
            self.script("return document.querySelector('[data-photos]').getAttribute('data-max-bytes');"), str(cap)
        )
        self.shoot("tient")
        self.assertEqual(len(self.script(PREVIEW_NAMES)), 1)
        self.assertIsNone(self.refusal())
        self.shoot("deborde")
        self.assertEqual(
            self.refusal(),
            f"Photo non ajoutée : l'envoi dépasserait {weight(cap)}. "
            "Importez d'abord ce qui est déjà choisi, puis reprenez-la.",
        )
        self.assertEqual(len(self.script(PREVIEW_NAMES)), 1)
        self.assertEqual(len(self.script(STORED_NAMES)), 1)
        self.assertEqual(self.script(f"return document.querySelector('{SLOT}').files.length;"), 0)
        self.assertLessEqual(self.script("return document.documentElement.scrollWidth;"), PHONE[0])
        # Taken back, the first leaves room, and the sentence goes.
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.assertIsNone(self.refusal())

    def test_what_a_shot_says_is_seen_with_its_tile(self):
        """The preview and the refusal sit right under the camera's tile,
        above the two other choices: under the whole grid, a phone drew them
        a screen below the tile tapped, and a refused shot went unseen."""
        with mock.patch("common.ONLINE_SEND_MAX_BYTES", len(tiny_jpeg())):
            self.open_the_card()
        self.script("window.scrollTo(0, 0);")
        self.shoot("vue")
        tile, preview, others = self.rects(".upload-choice-camera", "[data-photo-previews] li", ".upload-choices")
        self.assertLess(preview["top"] - tile["bottom"], 40)
        self.assertLessEqual(preview["bottom"], others["top"])
        # Seen together: the tile on the screen, the preview starting on it.
        self.assertGreaterEqual(tile["top"], 0)
        self.assertLess(preview["top"], PHONE[1])
        self.shoot("refusee")
        tile, refusal, preview = self.rects(".upload-choice-camera", "[data-photo-refused]", "[data-photo-previews] li")
        self.assertIsNotNone(self.refusal())
        self.assertLess(refusal["top"] - tile["bottom"], 40)
        self.assertLessEqual(refusal["bottom"], preview["top"])
        self.assertGreaterEqual(tile["top"], 0)
        self.assertLessEqual(refusal["bottom"], PHONE[1])

    def test_a_pick_past_the_cap_after_a_shot_is_emptied_and_said(self):
        """Shots first, then « Des fichiers »: the pick that would take the
        post past what one post may carry is emptied and said - weighed only
        at the next shot, « Importer » sent past it and Cloudflare's refusal
        took the shots with it."""
        cap = len(tiny_jpeg()) + 1
        with mock.patch("common.ONLINE_SEND_MAX_BYTES", cap):
            self.open_the_card()
        self.shoot("tient")
        self.script("window.scrollTo(0, document.documentElement.scrollHeight);")
        self.driver.find_element("css selector", FILES).send_keys(self.document("facture.pdf", b"%P"))
        self.wait_for(lambda: self.refusal() is not None)
        self.assertEqual(
            self.refusal(),
            f"Fichier non ajouté : l'envoi dépasserait {weight(cap)}. "
            "Importez d'abord ce qui est déjà choisi, puis choisissez-le à nouveau.",
        )
        self.assertEqual(self.script(f"return document.querySelector('{FILES}').files.length;"), 0)
        # The shot stays, and the sentence is brought into view.
        self.assertEqual(len(self.script(PREVIEW_NAMES)), 1)
        self.assertEqual(len(self.script(STORED_NAMES)), 1)
        (refusal,) = self.rects("[data-photo-refused]")
        self.assertGreaterEqual(refusal["top"], 0)
        self.assertLessEqual(refusal["bottom"], PHONE[1])
        # A pick that fits beside the shot is kept, and the sentence goes.
        self.driver.find_element("css selector", FILES).send_keys(self.document("note.pdf", b"%"))
        self.wait_for(lambda: self.refusal() is None)
        self.assertEqual(self.script(f"return document.querySelector('{FILES}').files.length;"), 1)
        self.assertEqual(len(self.script(PREVIEW_NAMES)), 1)

    def test_importer_past_the_cap_sends_nothing_while_a_shot_waits(self):
        """The last line: a selection that changed with no "change" event
        (set here through DataTransfer, which fires none) is weighed at
        « Importer », and nothing is sent - the button left as it was."""
        cap = len(tiny_jpeg()) + 1
        with mock.patch("common.ONLINE_SEND_MAX_BYTES", cap):
            self.open_the_card()
        self.shoot("attend")
        self.script(
            "var transfer = new DataTransfer();"
            "transfer.items.add(new File(['ab'], 'facture.pdf', {type: 'application/pdf'}));"
            "document.querySelector(arguments[0]).files = transfer.files;",
            FILES,
        )
        self.script("document.querySelector('form[data-receipt-upload] button[type=submit]').click();")
        self.wait_for(lambda: self.refusal() is not None)
        self.assertEqual(
            self.refusal(),
            f"Envoi arrêté : il dépasserait {weight(cap)}, et les photos prises seraient perdues. "
            "Retirez des photos ou choisissez moins de fichiers, puis « Importer » à nouveau.",
        )
        time.sleep(1)
        self.assertFalse(ReceiptBatch.objects.exists())
        self.assertEqual(self.script("return location.pathname;"), self.url)
        self.assertFalse(
            self.script("return document.querySelector('form[data-receipt-upload] button[type=submit]').disabled;")
        )
        self.assertEqual(len(self.script(PREVIEW_NAMES)), 1)
        self.assertEqual(len(self.script(STORED_NAMES)), 1)
        # Taken back, the shot no longer holds anything: the files go.
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.script("document.querySelector('form[data-receipt-upload] button[type=submit]').click();")
        self.wait_for(lambda: ReceiptBatch.objects.exists())

    def test_leaving_with_a_shot_waiting_asks_first(self):
        """A shot lives in the page alone: leaving with one waiting asks
        first (the browser's prompt: the « beforeunload » event cancelled),
        and with none it does not."""
        leave = (
            "var e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e); return e.defaultPrevented;"
        )
        self.assertFalse(self.script(leave))
        self.shoot("en-attente")
        self.assertTrue(self.script(leave))
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.assertFalse(self.script(leave))

    def test_shots_lost_with_the_page_are_said_when_it_comes_back(self):
        """Android may drop the tab while its camera app is in front, and the
        page comes back empty: how many shots waited is noted for the tab,
        and said once on the empty card - as Consignes says « N photos à
        reprendre »."""
        self.shoot("une")
        self.shoot("deux", TAKEN + 1)
        self.driver.refresh()
        self.wait_for(lambda: self.script("return document.readyState") == "complete")
        self.assertEqual(self.script(PREVIEW_NAMES), [])
        self.assertEqual(
            self.refusal(),
            "2 photos prises n'ont pas été importées : la page a été quittée ou rechargée avant « Importer ». "
            "Reprenez-les.",
        )
        # Said once.
        self.driver.refresh()
        self.wait_for(lambda: self.script("return document.readyState") == "complete")
        self.assertIsNone(self.refusal())

    def test_the_weight_said_is_the_server_s(self):
        """photos.js says the cap the way common.weight does - Ko under a
        megabyte, tenths rounded half to even, a comma - so the page and the
        server's own refusals never write one weight two ways."""
        sizes = [
            0,
            1,
            1023,
            1024,
            1025,
            300 * 1024,
            MEGABYTE - 1,
            MEGABYTE,
            MEGABYTE + 1,
            MEGABYTE * 5 // 4,
            MEGABYTE * 7 // 4,
            25 * MEGABYTE,
            90 * MEGABYTE,
            612 * MEGABYTE + MEGABYTE // 2,
            500 * MEGABYTE,
        ]
        said = self.script("return arguments[0].map(function (n) { return MarginMatePhotos.weight(n); });", sizes)
        self.assertEqual(said, [weight(size) for size in sizes])

    # -- the post -------------------------------------------------------------------------------------------

    def test_importer_posts_the_shots_under_their_new_names(self):
        self.assertEqual(self.busy_label(), "Envoi…")
        self.shoot("une")
        self.assertEqual(self.busy_label(), PHOTOS_BUSY)
        # Every photo taken back: the plain label again.
        self.script("document.querySelector('[data-photo-previews] li button').click();")
        self.assertEqual(self.busy_label(), "Envoi…")
        self.shoot("deux", TAKEN)
        self.shoot("trois", TAKEN + 61)
        names = self.script(PREVIEW_NAMES)
        self.assertEqual(names, [self.stamp() + ".jpg", self.stamp(TAKEN + 61) + ".jpg"])
        self.assertEqual(self.busy_label(), PHOTOS_BUSY)
        self.script("document.querySelector('form[data-receipt-upload] button[type=submit]').click();")
        self.wait_for(lambda: ReceiptBatch.objects.exists())
        batch = ReceiptBatch.objects.get()
        # The answer is the import's own page: the files are staged by then.
        target = reverse("invoices:receipt_batch", args=[batch.pk])
        self.wait_for(lambda: self.script("return location.pathname;") == target)
        batch.refresh_from_db()
        self.assertEqual(
            [(entry["name"], entry["status"]) for entry in batch.results], [(name, "pending") for name in names]
        )
        for name in names:
            self.assertRegex(name, RENAMED)
        self.started.assert_called_once()


@tag("browser")
class CameraOnADesktopInBrowserTests(CameraBrowserTestCase):
    def test_a_desktop_draws_no_camera_tile(self):
        """A desktop browser ignores `capture`: the tile would only open a
        file dialog beside « Des fichiers ». The window is pinned - headless
        Chrome's 800 × 600 is under 860 px - and no touch is emulated."""
        self.device(*DESKTOP, touch=False)
        self.open_the_card()
        self.assertFalse(self.script("return window.matchMedia('(pointer: coarse)').matches;"))
        self.assertEqual(
            self.script("return getComputedStyle(document.querySelector('.upload-choice-camera')).display;"), "none"
        )
        self.assertFalse(self.drawn(".upload-choice-camera"))
        drawn = self.script(
            "return Array.from(document.querySelectorAll('.upload-choices > .upload-choice'))"
            ".filter(function (e) { return e.getClientRects().length > 0; })"
            ".map(function (e) { return e.querySelector('.upload-choice-title').textContent; });"
        )
        self.assertEqual(drawn, ["Des fichiers", "Un dossier entier"])

    def test_with_no_shot_the_other_choices_post_past_the_cap(self):
        """What one post may carry guards the shots, which a refused post
        loses; with none waiting nothing is held - a folder imported on the
        PC itself never meets Cloudflare, and goes to the server's own cap."""
        self.device(*DESKTOP, touch=False)
        with mock.patch("common.ONLINE_SEND_MAX_BYTES", 1):
            self.open_the_card()
        self.driver.find_element("css selector", FILES).send_keys(self.document("ticket.jpg", tiny_jpeg()))
        self.assertEqual(self.script(f"return document.querySelector('{FILES}').files.length;"), 1)
        self.script("document.querySelector('form[data-receipt-upload] button[type=submit]').click();")
        self.wait_for(lambda: ReceiptBatch.objects.exists())
        batch = ReceiptBatch.objects.get()
        target = reverse("invoices:receipt_batch", args=[batch.pk])
        self.wait_for(lambda: self.script("return location.pathname;") == target)
        batch.refresh_from_db()
        self.assertEqual([entry["name"] for entry in batch.results], ["ticket.jpg"])
