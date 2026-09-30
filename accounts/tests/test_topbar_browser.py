"""The topbar of a logged-in page, in a real (headless) Chrome.

Two bars since 30/09 (the owner: « the top menu is too big, maybe do
something that can be expanded »; templates/base.html, static/js/topbar.js,
marginmate.css « topbar menu »):

* **Under 860 px, once topbar.js has run** (the class `topbar-menu-ready` on
  <html>), the links, the bar's name and « Se déconnecter » fold into « Menu »
  and the bar is ONE row - the brand, the page's section, the button - where
  the links took three to five, sticky on every page (141 px at 375, 199 at
  280). Opened, the menu drops over the page from under the bar, veiled;
  Escape, a tap on the veil, the focus leaving the bar, a wider window and a
  page shown again from the browser's memory shut it.
* **Above 860 px, and wherever the script did not run**, the bar is exactly
  the one measured before: every link, the bar's name and « Se déconnecter »
  beside them, no taller than the room the page leaves above what it scrolls
  to (--topbar-room; invoices.tests.test_changes_to_see.
  TopbarRoomInBrowserTests measures where the pages scroll to). The bar
  without the script is measured in this same Chrome with the class taken
  off <html> - the markup and the stylesheet a browser with JavaScript off
  gets, since the stylesheet folds nothing without the class.

Measured at the widths where the bar changes shape, with a three-digit badge
on each counting link (they are what makes the links wrap) and bar names of
two lengths. Under 860 px Chrome is a phone - a mobile viewport, touch - and
a tap or a drag is a finger's (CDP touch events); above it, a computer's
window, up to 1440 px, where the bar with the script is box for box the bar
without it.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile
import time
from html import unescape

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.scrapers import website
from recipes.models import PosProduct
from tests.factories import make_invoice, make_product, make_supplier
from tests.test_navigation import LABELS

#: The topbar's @media (max-width: …) that folds the links (marginmate.css,
#: « topbar menu »): one figure for every assertion here.
FOLD_MAX = 860
#: 1440: a computer's screen wider than the page's content (1240 px), where
#: the bar is centred - the owner's laptop.
WIDTHS = (1440, 1280, 1100, 1000, 960, 900, 861, 860, 768, 600, 465, 450, 375, 334, 320, 310, 300, 280)
WIDE = tuple(width for width in WIDTHS if width > FOLD_MAX)
NARROW = tuple(width for width in WIDTHS if width <= FOLD_MAX)
#: Folded, the bar is one row: 57 px measured (the 44 px button, its
#: padding, the border). Two rows of anything would be past this.
ONE_ROW = 64
#: The room a one-row bar leaves, 6rem (html.topbar-menu-ready, « topbar
#: menu »): the 10, 11 and 13rem of the bar that wraps would leave a heading
#: the page scrolls to far below a bar of 57 px.
FOLDED_ROOM = 96
#: A thumb's target (the Consignes pages' rule).
THUMB = 44
#: From here up the bar names the page's section beside the brand
#: (@media (max-width: 339px) gives it way).
SECTION_FROM = 340
LINKS = [unescape(label) for label in LABELS]
LONG_NAME = "Le Comptoir des Essais et de la Dégustation du Quartier"
#: A name of common length, then one past the 12rem the name may take (cut
#: with « … »), then the same for a superuser, whose links carry « Admin ».
BARS = (("Le Comptoir des Essais", False), (LONG_NAME, False), (LONG_NAME, True))

#: The bar as it was measured before the fold.
MEASURE = """
const drawn = (e) => !!e && e.getClientRects().length > 0;
const bar = document.querySelector('.topbar').getBoundingClientRect();
const room = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop);
const brand = document.querySelector('.brand').getBoundingClientRect();
const account = document.querySelector('.topbar-account');
const button = account.querySelector('button').getBoundingClientRect();
const name = account.querySelector('.topbar-espace');
return {
    ready: document.documentElement.classList.contains('topbar-menu-ready'),
    height: bar.height, room: room,
    brandTop: brand.top, brandBottom: brand.bottom,
    buttonTop: button.top, buttonBottom: button.bottom, buttonLeft: button.left, buttonRight: button.right,
    buttonWidth: button.width, viewport: document.documentElement.clientWidth,
    nameShown: getComputedStyle(name).display !== 'none' ? name.getBoundingClientRect().width : 0,
    linksDrawn: Array.from(document.querySelectorAll('.topbar nav a')).filter(drawn).length,
    toggleDrawn: drawn(document.querySelector('[data-topbar-toggle]')),
    sectionDrawn: drawn(document.querySelector('.topbar-section')),
};
"""

#: The bar folded: what is drawn on its one row.
FOLDED = """
var drawn = function (e) { return !!e && e.getClientRects().length > 0; };
var box = function (e) {
    var r = e.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
};
var toggle = document.querySelector('[data-topbar-toggle]');
var section = document.querySelector('.topbar-section');
return {
    ready: document.documentElement.classList.contains('topbar-menu-ready'),
    bar: box(document.querySelector('.topbar')),
    room: parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop),
    brand: box(document.querySelector('.brand')),
    toggle: drawn(toggle) ? box(toggle) : null,
    expanded: toggle ? toggle.getAttribute('aria-expanded') : null,
    section: drawn(section) ? Object.assign(box(section), {text: section.textContent.trim()}) : null,
    dot: drawn(document.querySelector('.topbar-waiting')),
    dotWords: drawn(document.querySelector('.topbar-waiting-text')),
    linksDrawn: Array.prototype.filter.call(document.querySelectorAll('.topbar nav a'), drawn).length,
    accountDrawn: drawn(document.querySelector('.topbar-espace')) || drawn(document.querySelector('.topbar-logout button')),
    barContent: document.querySelector('.topbar-inner').scrollWidth,
};
"""

#: What a finger finds: drawn, on the screen, and the topmost thing at its
#: middle (the menu is over the page, the veil over the rest).
HIT = """
var hit = function (e) {
    var r = e.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    var x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
    var found = document.elementFromPoint(x, y);
    return !!found && (found === e || e.contains(found));
};
var box = function (e) {
    var r = e.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
};
var item = function (e, label) { return Object.assign(box(e), {label: label, reachable: hit(e)}); };
"""

#: The menu opened: every item of it, where it is and whether a finger finds it.
OPENED = HIT + """
var name = document.querySelector('.topbar-espace');
return {
    bar: box(document.querySelector('.topbar')),
    menu: box(document.getElementById('topbar-menu')),
    links: Array.prototype.map.call(document.querySelectorAll('.topbar nav a'), function (a) {
        return item(a, a.firstChild.textContent.trim());
    }),
    name: item(name, name.textContent.trim()),
    logout: item(document.querySelector('.topbar-logout button'), 'Se déconnecter'),
};
"""

#: Where the page is under the bar.
PAGE_POSITION = "return [window.scrollY, document.querySelector('main').getBoundingClientRect().top];"

#: Everything the bar draws, box by box (to the half pixel), and what
#: #topbar-menu is: the bar with topbar.js and the bar without it compared.
LAYOUT = """
var boxes = function (css) {
    return Array.prototype.map.call(document.querySelectorAll(css), function (e) {
        var r = e.getBoundingClientRect();
        return [css, Math.round(r.left * 2) / 2, Math.round(r.top * 2) / 2,
                Math.round(r.width * 2) / 2, Math.round(r.height * 2) / 2];
    });
};
var menu = document.getElementById('topbar-menu');
return {
    menu: menu ? getComputedStyle(menu).display : null,
    boxes: [].concat(boxes('.topbar'), boxes('.brand'), boxes('.topbar nav'), boxes('.topbar nav a'),
                     boxes('.topbar nav .badge'), boxes('.topbar-account'), boxes('.topbar-espace'),
                     boxes('.topbar-logout button')),
};
"""

#: How wide the page is, and - when it is wider than the screen
#: (arguments[0] px) - the first elements past the screen's edge whose
#: parent is not, outside any box that scrolls or clips sideways: where the
#: overflow starts, named in a failure.
PAGE_OVERFLOW = """
var width = arguments[0], page = document.documentElement.scrollWidth, from = [];
if (page > width) {
    var held = function (e) {
        for (var a = e.parentElement; a && a !== document.body; a = a.parentElement) {
            if (getComputedStyle(a).overflowX !== 'visible') return true;
        }
        return false;
    };
    Array.prototype.forEach.call(document.querySelectorAll('body *'), function (e) {
        if (from.length >= 5 || !e.getClientRects().length) return;
        var right = e.getBoundingClientRect().right;
        var parent = e.parentElement ? e.parentElement.getBoundingClientRect().right : 0;
        if (right > width + 0.5 && parent <= width + 0.5 && !held(e)) {
            var name = typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\\s+/).join('.') : '';
            from.push(e.tagName.toLowerCase() + name + ' (to ' + Math.round(right) + ' px)');
        }
    });
}
return {page: page, from: from};
"""


class TopbarInChrome:
    """One Chrome per class; a phone at or under FOLD_MAX, a computer's
    window above it - touch follows, so (hover) and (pointer) never leak
    from one width to the next."""

    HEIGHT = 700
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

    def log_in(self):
        """The bar's member, its session handed to Chrome (a
        TransactionTestCase empties the sessions after every test)."""
        self.client.force_login(self.user)
        self.driver.get(self.live_server_url + reverse("accounts:login"))
        self.driver.add_cookie({"name": "sessionid", "value": self.client.cookies["sessionid"].value, "path": "/"})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})

    def bar_named(self, name, superuser=False):
        type(self.bar).objects.filter(pk=self.bar.pk).update(name=name)
        type(self.user).objects.filter(pk=self.user.pk).update(is_superuser=superuser, is_staff=superuser)

    def device(self, width, height=None):
        phone = width <= FOLD_MAX
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height or self.HEIGHT, "deviceScaleFactor": 2 if phone else 1, "mobile": phone},
        )
        touch = {"enabled": True, "maxTouchPoints": 5} if phone else {"enabled": False}
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", touch)

    def open(self, path=None):
        self.driver.get(self.live_server_url + (path or reverse("invoices:invoice_list")))
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def expanded(self):
        """« Menu »'s aria-expanded - None where there is no « Menu »."""
        return self.script(
            "var toggle = document.querySelector('[data-topbar-toggle]');"
            "return toggle ? toggle.getAttribute('aria-expanded') : null;"
        )

    def middle(self, css):
        box = self.script(
            "var r = document.querySelector(arguments[0]).getBoundingClientRect();"
            "return [r.left + r.width / 2, r.top + r.height / 2];",
            css,
        )
        return box[0], box[1]

    def touch(self, x, y):
        """A finger's tap at (x, y), as the phone sends it."""
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def tap(self, css):
        self.touch(*self.middle(css))

    def drag(self, x, y, to_y, steps=12):
        """A finger put down at (x, y), dragged to (x, to_y) and held there
        before it lifts: no fling goes on scrolling after the test looked."""
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for step in range(1, steps + 1):
            at = y + (to_y - y) * step / steps
            self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": at}]})
        time.sleep(0.3)
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": to_y}]})
        self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def still(self, source):
        """What `source` returns once three reads 0.1 s apart agree: a
        scroll a finger started has ended."""
        seen = []

        def settled():
            seen.append(self.script(source))
            time.sleep(0.1)
            return len(seen) > 2 and seen[-1] == seen[-2] == seen[-3]

        self.wait_for(settled)
        return seen[-1]

    def click_at(self, x, y):
        """A mouse's click at (x, y), whatever is there."""
        for kind in ("mousePressed", "mouseReleased"):
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            )

    def open_the_menu(self):
        self.assertEqual(self.expanded(), "false", "« Menu » drawn shut")
        self.tap("[data-topbar-toggle]")
        self.wait_for(lambda: self.expanded() == "true")

    def folded(self):
        return self.script(FOLDED)


@tag("browser")
class TopbarOfAnEspaceInBrowserTests(TopbarInChrome, TenancyTestCase, StaticLiveServerTestCase):
    """The bar's shape width by width: the bar that was (above 860 px, and
    at every width without the script) and the folded one."""

    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True
    # Every problem of every width said, not the first few.
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.bar = self.make_tenant("Le Comptoir des Essais")
        self.user = self.make_member(self.bar, "gerante@example.invalid")
        with bound_tenant(self.bar):
            caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
            # Three-digit badges: the widest the links get.
            for n in range(120):
                make_product(supplier=caterer, raw_name=f"PRODUIT À CLASSER {n:03d}")
            for n in range(110):
                make_invoice(
                    supplier=caterer,
                    invoice_number=f"T-{n:03d}",
                    parse_checks=[{"label": "Total du ticket", "passed": False, "detail": ""}],
                )
            for n in range(105):
                PosProduct.objects.create(name=f"Produit caisse {n:03d}", total_quantity=3)
        self.log_in()

    def assertTheBarIsAsItWas(self, widths, *, without_the_script):
        """The bar measured before the fold (28/09): no taller than its room,
        every link drawn, « Se déconnecter » on the screen - on the brand's
        row under 860 px - and the bar's name at least 50 px wide from 320 px
        up; « Menu » and the section's name drawn nowhere."""
        page = self.live_server_url + reverse("invoices:invoice_list")
        rows, problems = [], []
        for name, superuser in BARS:
            self.bar_named(name, superuser)
            rows.append(f"{name}{' (superuser)' if superuser else ''}")
            links = len(LINKS) + (1 if superuser else 0)
            for width in widths:
                self.device(width)
                self.driver.get(page)
                if without_the_script:
                    self.script("document.documentElement.classList.remove('topbar-menu-ready');")
                m = self.script(MEASURE)
                rows.append(f"{width:>5} px: bar {m['height']:.0f} / room {m['room']:.0f}, name {m['nameShown']:.0f} px")
                where = f"{name[:22]}…, {width} px"
                if m["ready"] == without_the_script:
                    problems.append(f"{where}: topbar-menu-ready on <html>: {m['ready']}")
                if m["height"] > m["room"]:
                    problems.append(f"{where}: the bar is {m['height']:.0f} px tall, the room {m['room']:.0f}")
                if m["linksDrawn"] != links:
                    problems.append(f"{where}: {m['linksDrawn']} links drawn of {links}")
                if m["toggleDrawn"] or m["sectionDrawn"]:
                    problems.append(f"{where}: « Menu » or the section's name drawn")
                if m["buttonWidth"] <= 0 or m["buttonRight"] > m["viewport"] or m["buttonLeft"] < 0:
                    problems.append(f"{where}: « Se déconnecter » is not on the screen")
                if width <= FOLD_MAX and not (m["buttonTop"] < m["brandBottom"] and m["buttonBottom"] > m["brandTop"]):
                    problems.append(f"{where}: « Se déconnecter » is not on the brand's row")
                if width >= 320 and m["nameShown"] < 50:
                    problems.append(f"{where}: the bar's name is {m['nameShown']:.0f} px wide")
                if not without_the_script:
                    # Box for box the bar a browser without the script draws.
                    drawn = self.script(LAYOUT)
                    self.script("document.documentElement.classList.remove('topbar-menu-ready');")
                    before = self.script(LAYOUT)
                    if drawn["menu"] != "contents":
                        problems.append(f"{where}: #topbar-menu is display: {drawn['menu']}")
                    if drawn["boxes"] != before["boxes"]:
                        moved = [(a, b) for a, b in zip(drawn["boxes"], before["boxes"]) if a != b]
                        problems.append(f"{where}: the bar is not the one without topbar.js: {moved[:3]}")
        self.assertEqual(problems, [], "\n".join(rows))

    def test_above_860_px_the_bar_is_as_it_was(self):
        """topbar.js runs on a computer too, and changes nothing there: the
        button and the section are not drawn, #topbar-menu is no box of its
        own (display: contents), and every box of the bar - the brand, each
        link and badge, the bar's name, « Se déconnecter » - is exactly
        where it is with the class taken off <html>, the bar a browser
        without the script draws. Up to a computer's 1440 px."""
        self.assertTheBarIsAsItWas(WIDE, without_the_script=False)

    def test_without_the_script_the_bar_is_as_it_was_at_every_width(self):
        """JavaScript off, or topbar.js missing: nothing folds, and the
        rooms measured for three to five rows of links (10, 11 and 13rem
        under 860 px) still hold the bar - no link out of reach behind a
        button that would do nothing."""
        self.assertTheBarIsAsItWas(WIDTHS, without_the_script=True)

    def test_under_860_px_the_links_fold_into_one_row(self):
        """Folded: one row in a one-row bar's room (6rem), no link, no name
        and no logout drawn; « Menu » a thumb's size and on the screen; a dot
        on it while a link carries a badge; the brand a thumb's height too;
        the page's section named between them from 340 px up; and nothing of
        the bar's row past the screen's edge. What the page under it does is
        the next test's."""
        page = self.live_server_url + reverse("invoices:invoice_list")
        rows, problems = [], []
        for name, superuser in BARS:
            self.bar_named(name, superuser)
            rows.append(f"{name}{' (superuser)' if superuser else ''}")
            for width in NARROW:
                self.device(width)
                self.driver.get(page)
                m = self.folded()
                rows.append(f"{width:>5} px: bar {m['bar']['height']:.0f} / room {m['room']:.0f}")
                where = f"{name[:22]}…, {width} px"
                toggle = m["toggle"]
                if not m["ready"] or toggle is None:
                    problems.append(f"{where}: not folded (topbar-menu-ready {m['ready']}, « Menu » drawn {bool(toggle)})")
                    continue
                if m["bar"]["height"] > ONE_ROW or m["bar"]["height"] > m["room"]:
                    problems.append(f"{where}: folded, the bar is {m['bar']['height']:.0f} px tall (room {m['room']:.0f})")
                if m["room"] != FOLDED_ROOM:
                    problems.append(f"{where}: the room is {m['room']:.0f} px, a one-row bar's is {FOLDED_ROOM}")
                if m["linksDrawn"] or m["accountDrawn"]:
                    problems.append(f"{where}: {m['linksDrawn']} links drawn, the account drawn: {m['accountDrawn']}")
                if m["expanded"] != "false":
                    problems.append(f"{where}: aria-expanded {m['expanded']!r}")
                if min(toggle["width"], toggle["height"]) < THUMB:
                    problems.append(f"{where}: « Menu » is {toggle['width']:.0f} × {toggle['height']:.0f} px")
                if toggle["left"] < 0 or toggle["right"] > width:
                    problems.append(f"{where}: « Menu » is off the screen ({toggle['left']:.0f} - {toggle['right']:.0f})")
                if m["brand"]["height"] < THUMB or m["brand"]["left"] < 0 or m["brand"]["right"] > toggle["left"]:
                    problems.append(f"{where}: the brand is {m['brand']['height']:.0f} px tall, or under « Menu »")
                if not (m["dot"] and m["dotWords"]):
                    problems.append(f"{where}: no dot on « Menu » with every counting link carrying a badge")
                section = m["section"]
                if width >= SECTION_FROM and (
                    section is None or section["text"] != "Achats" or section["width"] < 30
                    or section["left"] < m["brand"]["right"] or section["right"] > toggle["left"]
                ):
                    problems.append(f"{where}: the section's name is {section}")
                if width < SECTION_FROM and section is not None:
                    problems.append(f"{where}: the section's name is drawn beside a brand that has no room")
                if m["barContent"] > width:
                    problems.append(f"{where}: the bar's row is {m['barContent']} px wide")
        self.assertEqual(problems, [], "\n".join(rows))

    def test_under_860_px_the_page_leaves_menu_on_the_screen(self):
        """A page wider than the phone is one Chrome lets the finger drag
        sideways, and the bar - « Menu » at its right, the one way to every
        link - goes out of view with the page's left edge (UX review, 30/09).
        So Achats, the page this file measures the bar on, is no wider than
        the screen at any width the bar folds at - and a failure names where
        the overflow starts. One bar name: folded, the name is in the shut
        menu, which takes no room."""
        page = self.live_server_url + reverse("invoices:invoice_list")
        problems = []
        for width in NARROW:
            self.device(width)
            self.driver.get(page)
            m = self.script(PAGE_OVERFLOW, width)
            if m["page"] > width:
                problems.append(f"{width} px: the page is {m['page']} px wide, from {m['from']}")
        self.assertEqual(problems, [])

    def test_at_280_px_a_bigger_font_leaves_menu_on_the_screen(self):
        """A folding phone's 280 px cover screen with the text at 125 %
        (Android's font size): the brand gives way before « Menu » does - the
        one way to every link must never be pushed off the screen."""
        self.device(280, 653)
        self.open()
        self.script("document.documentElement.style.fontSize = '125%';")
        m = self.folded()
        self.assertIsNotNone(m["toggle"], m)
        self.assertGreaterEqual(m["toggle"]["left"], 0, m)
        self.assertLessEqual(m["toggle"]["right"], 280, m)
        self.assertGreaterEqual(m["toggle"]["height"], THUMB, m)
        self.assertLessEqual(m["brand"]["right"], m["toggle"]["left"], m)
        self.assertLessEqual(m["bar"]["height"], m["room"], m)
        # The bar's own row holds at that size: nothing of it runs past the
        # screen (what the page under it does at 125 % is the page's).
        self.assertLessEqual(m["barContent"], 280, m)
        self.open_the_menu()
        self.assertTrue(all(link["reachable"] for link in self.script(OPENED)["links"][:3]))

    def test_on_every_phone_a_bigger_font_leaves_menu_on_the_screen(self):
        """Android's text scaling at 150 and 200 % (the root font, as above)
        on phones of 360 to 412 px: the brand could not shrink above 340 px
        and the section asked for its own width first, so the bar's one row
        was wider than the phone - 375 px at 360 px and 150 %, 500 px at
        every one of them at 200 % - with « Menu », the one way to every link,
        partly off the screen and the page dragged sideways (review of
        30/09). The section gives way, then the brand, never « Menu ».
        Measured on Produits & charges, the longest section's name, a page
        whose own content fits at those sizes (Achats' is wider at 200 %:
        that is the page's, not the bar's). Chrome widens its viewport to a
        page wider than the phone, so every edge is held to the phone's own
        width, never to the viewport's."""
        page = reverse("inventory:stock_list")
        rows, problems = [], []
        for width in (360, 375, 393, 412):
            self.device(width, 800)
            for scale in ("150%", "200%"):
                self.open(page)
                self.script("document.documentElement.style.fontSize = arguments[0];", scale)
                m = self.folded()
                overflow = self.script(PAGE_OVERFLOW, width)
                where = f"{width} px, {scale}"
                toggle = m["toggle"]
                rows.append(f"{where}: « Menu » {toggle and (round(toggle['left']), round(toggle['right']))}, "
                            f"row {m['barContent']}, page {overflow['page']}")
                if toggle is None:
                    problems.append(f"{where}: no « Menu »")
                    continue
                if toggle["left"] < 0 or toggle["right"] > width:
                    problems.append(f"{where}: « Menu » at {toggle['left']:.0f} - {toggle['right']:.0f}, off the screen")
                if min(toggle["width"], toggle["height"]) < THUMB:
                    problems.append(f"{where}: « Menu » is {toggle['width']:.0f} × {toggle['height']:.0f} px")
                if m["brand"]["right"] > toggle["left"] + 0.5:
                    problems.append(f"{where}: the brand runs under « Menu »")
                if m["barContent"] > width:
                    problems.append(f"{where}: the bar's row is {m['barContent']} px wide")
                if overflow["page"] > width:
                    problems.append(f"{where}: the page is {overflow['page']} px wide, from {overflow['from']}")
        self.assertEqual(problems, [], "\n".join(rows))

    def test_at_the_default_font_the_brand_is_whole_beside_the_section(self):
        """What lets the brand give way (above) never cuts it where there is
        room: the section only takes the room left and gives way first. At
        the default font, from 340 px up, the brand is whole beside
        « Produits & charges », the longest section's name - shrinking with
        the section in proportion to their widths cut it to « MarginM… » at
        340 to 375 px (a variant the review of 30/09 measured)."""
        page = reverse("inventory:stock_list")
        problems = []
        for width in (340, 360, 375, 412, 600, 860):
            self.device(width)
            self.open(page)
            m = self.folded()
            brand = self.script("var b = document.querySelector('.brand'); return [b.scrollWidth, b.clientWidth];")
            if brand[0] > brand[1]:
                problems.append(f"{width} px: the brand is cut ({brand[0]} px in {brand[1]})")
            if m["section"] is None or m["section"]["text"] != "Produits & charges":
                problems.append(f"{width} px: the section's name is {m['section']}")
        self.assertEqual(problems, [])


@tag("browser")
class TopbarMenuInBrowserTests(TopbarInChrome, TenancyTestCase, StaticLiveServerTestCase):
    """« Menu » at work on a phone: what it opens, where, what shuts it,
    what it says. A dozen rows: three products to classify, two tickets to
    check, one till product to link - a badge on each counting link."""

    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    PHONE = (375, 700)

    def setUp(self):
        super().setUp()
        self.bar = self.make_tenant(LONG_NAME)
        self.user = self.make_member(self.bar, "gerante@example.invalid")
        with bound_tenant(self.bar):
            caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
            for n in range(3):
                make_product(supplier=caterer, raw_name=f"PRODUIT À CLASSER {n}")
            for n in range(6):
                make_invoice(
                    supplier=caterer,
                    invoice_number=f"T-{n}",
                    parse_checks=[{"label": "Total du ticket", "passed": n >= 2, "detail": ""}],
                )
            PosProduct.objects.create(name="Produit caisse à lier", total_quantity=3)
        self.log_in()

    def test_opened_the_menu_holds_every_link_the_name_and_the_logout_over_the_page(self):
        """Every link (« Admin » too, a superuser's) a thumb's height, on the
        screen and found by a finger; the bar's name and « Se déconnecter »
        at its foot; the menu dropping from the bar's bottom OVER the page,
        which does not move - grown in place, a sticky bar would push the
        page down by the menu's height and back. A second tap shuts it."""
        self.bar_named(LONG_NAME, superuser=True)
        for width, height in ((375, 812), (768, 1024)):  # a phone, a tablet
            with self.subTest(width=width):
                self.device(width, height)
                self.open()
                self.script("window.scrollTo(0, 150);")
                before = self.script(PAGE_POSITION)
                self.assertGreater(before[0], 0, "the page did not scroll: nothing to prove")
                self.open_the_menu()
                m = self.script(OPENED)
                self.assertEqual(self.script(PAGE_POSITION), before)
                self.assertLessEqual(abs(m["menu"]["top"] - m["bar"]["bottom"]), 1, m)
                self.assertEqual([link["label"] for link in m["links"]], [*LINKS, "Admin"])
                for item in [*m["links"], m["logout"]]:
                    with self.subTest(item=item["label"]):
                        self.assertTrue(item["reachable"], item)
                        self.assertGreaterEqual(item["height"], THUMB, item)
                        self.assertGreaterEqual(item["left"], 0, item)
                        self.assertLessEqual(item["right"], width, item)
                        self.assertGreaterEqual(item["top"], m["bar"]["bottom"] - 1, item)
                        self.assertLessEqual(item["bottom"], height, item)
                self.assertTrue(m["name"]["reachable"], m["name"])
                self.assertEqual(m["name"]["label"], LONG_NAME)
                self.assertGreaterEqual(m["name"]["width"], 50, m["name"])
                self.assertLessEqual(m["name"]["right"], width, m["name"])
                self.assertLess(m["links"][-1]["bottom"], m["name"]["top"], "the account at the menu's foot")
                self.assertLess(m["name"]["bottom"], m["logout"]["top"] + 1, "the name over « Se déconnecter »")
                # Room under « Se déconnecter », the last item, before the
                # veil: a thumb slipping onto the veil to shut the menu must
                # not land on the logout (marginmate.css, « topbar menu »).
                self.assertGreaterEqual(m["menu"]["bottom"] - m["logout"]["bottom"], 20, m)
                self.tap("[data-topbar-toggle]")
                self.wait_for(lambda: self.expanded() == "false")
                self.assertEqual(self.folded()["linksDrawn"], 0)
                self.assertEqual(self.script(PAGE_POSITION), before)

    def test_on_its_side_the_phone_reaches_the_menu_s_last_item(self):
        """A phone on its side: ten links and the account are taller than
        the screen - by a few pixels at 667 × 375 (three columns of links),
        by a third of it at 568 × 320 (two). The bar sticks, so the menu
        scrolls itself: every item can be brought into view inside it - the
        page behind staying where it is - and a finger then finds it."""
        self.bar_named(LONG_NAME, superuser=True)
        for width, height in ((667, 375), (568, 320)):
            with self.subTest(width=width):
                self.device(width, height)
                self.open()
                self.open_the_menu()
                menu = self.script(OPENED)["menu"]
                self.assertLessEqual(menu["bottom"], height, menu)
                self.assertGreater(
                    self.script("var m = document.getElementById('topbar-menu'); return m.scrollHeight - m.clientHeight;"), 0,
                    "the menu fits the screen: nothing to prove",
                )
                before = self.script(PAGE_POSITION)
                found = self.script(
                    HIT + """
                    var items = Array.prototype.slice.call(document.querySelectorAll('.topbar nav a, .topbar-logout button'));
                    return items.map(function (e) {
                        e.scrollIntoView({block: 'nearest'});
                        return [e.textContent.trim(), hit(e)];
                    });"""
                )
                self.assertEqual(len(found), len(LINKS) + 2)
                self.assertEqual([label for label, reachable in found if not reachable], [])
                self.assertEqual(self.script(PAGE_POSITION), before)

    def test_a_finger_scrolls_the_open_menu_and_never_the_page(self):
        """568 × 320, a small phone on its side: the menu is taller than the
        screen and a finger dragged on it scrolls IT - to its end and past -
        while the page behind stays where it was (`overscroll-behavior:
        contain`: a menu handing its scroll on to the page moves what is
        under the veil). Whatever keeps a finger on the veil from scrolling
        the page must leave this one alone: the menu is inside the bar the
        veil belongs to."""
        self.bar_named(LONG_NAME, superuser=True)
        self.device(568, 320)
        self.open()
        self.script("window.scrollTo(0, 100);")
        page = self.still("return window.scrollY;")
        self.assertGreater(page, 0, "the page did not scroll: nothing to prove")
        self.open_the_menu()
        menu = self.script(OPENED)["menu"]
        scrolled = "return document.getElementById('topbar-menu').scrollTop;"
        room = self.script("var m = document.getElementById('topbar-menu'); return m.scrollHeight - m.clientHeight;")
        self.assertGreater(room, 60, "the menu fits the screen: nothing to prove")
        x, y = menu["left"] + menu["width"] / 2, menu["bottom"] - 20
        self.drag(x, y, y - 120)
        self.assertGreater(self.still(scrolled), 30, "a finger on the open menu did not scroll it")
        self.assertEqual(self.still("return window.scrollY;"), page)
        for _ in range(3):  # to its end, and on past it
            self.drag(x, y, menu["top"] + 10)
        self.assertGreaterEqual(self.still(scrolled), room - 1)
        self.assertEqual(self.still("return window.scrollY;"), page, "the menu handed its scroll on to the page")
        self.assertEqual(self.expanded(), "true")

    def test_escape_and_the_focus_leaving_the_bar_shut_it(self):
        """Escape shuts it and gives the focus back to « Menu » when it was
        in the bar; Tab past « Se déconnecter » shuts it - a menu left open
        over the page hides the field being typed in."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        self.device(*self.PHONE)
        self.open()
        self.open_the_menu()
        self.script("document.querySelector('.topbar nav a').focus();")
        ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
        self.wait_for(lambda: self.expanded() == "false")
        self.assertTrue(self.script("return document.activeElement.matches('[data-topbar-toggle]');"))
        self.assertEqual(self.folded()["linksDrawn"], 0)

        self.open_the_menu()
        self.script("document.querySelector('.topbar-logout button').focus();")
        ActionChains(self.driver).send_keys(Keys.TAB).perform()
        self.wait_for(lambda: self.expanded() == "false")
        self.assertFalse(self.script("return !!document.activeElement.closest('.topbar');"))
        self.assertEqual(self.folded()["linksDrawn"], 0)

    def test_walked_by_keyboard_every_item_shows_its_focus(self):
        """Opened with Enter and walked with Tab, each item of the menu draws
        the app's focus ring - « Se déconnecter » too: a .link-button, whose
        `all: unset` had taken the ring away, so the last item of the menu
        showed no focus at all (review of 30/09)."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        focused = (
            "var e = document.activeElement, s = getComputedStyle(e);"
            "return {item: e.textContent.trim(), ring: e.matches(':focus-visible'),"
            "  outline: s.outlineStyle + ' ' + s.outlineWidth, logout: !!e.closest('.topbar-logout')};"
        )
        self.device(*self.PHONE)
        self.open()
        self.script("document.querySelector('[data-topbar-toggle]').focus();")
        ActionChains(self.driver).send_keys(Keys.ENTER).perform()
        self.wait_for(lambda: self.expanded() == "true")
        walked = []
        for _ in range(len(LINKS) + 5):
            ActionChains(self.driver).send_keys(Keys.TAB).perform()
            walked.append(self.script(focused))
            if walked[-1]["logout"]:
                break
        self.assertTrue(walked[-1]["logout"], f"Tab never reached « Se déconnecter »: {walked}")
        self.assertEqual(len(walked), len(LINKS) + 1, walked)
        self.assertEqual([item for item in walked if not (item["ring"] and item["outline"] == "solid 2px")], [])
        self.assertEqual(self.expanded(), "true")

    def test_a_click_on_the_veil_shuts_it_and_reaches_nothing_under_it(self):
        """The page is veiled under the open menu (the bar's ::after), and a
        click there only shuts the menu: on a phone the tap that closes a
        menu would otherwise follow the link under the finger. A link of the
        page is put where the veil will be, bottom left."""
        width, height = self.PHONE
        self.device(width, height)
        self.open()
        self.script(
            "var a = document.createElement('a'); a.id = 'sous-le-voile'; a.href = arguments[0];"
            " a.textContent = 'Banque';"
            " Object.assign(a.style, {position: 'fixed', left: '0', bottom: '0', width: '120px', height: '48px',"
            " display: 'block'});"
            " document.querySelector('main').appendChild(a); window.pageMark = 'toujours là';",
            reverse("bank:bank_home"),
        )
        x, y = 20, height - 12
        under = "var e = document.elementFromPoint(arguments[0], arguments[1]); return e ? (e.id || e.className) : null;"
        self.assertEqual(self.script(under, x, y), "sous-le-voile")
        self.open_the_menu()
        self.assertLess(self.script(OPENED)["menu"]["bottom"], y - 8, "the point is under the menu, not the veil")
        self.assertEqual(self.script(under, x, y), "topbar")
        self.click_at(x, y)
        self.wait_for(lambda: self.expanded() == "false")
        time.sleep(0.5)   # a link followed would have left the page by now
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        self.assertEqual(self.script("return location.pathname;"), reverse("invoices:invoice_list"))
        # Shut, the veil is gone: the link is what a click there reaches again.
        self.assertEqual(self.script(under, x, y), "sous-le-voile")

    def test_widened_or_shown_again_it_comes_back_shut(self):
        """Widened past 860 px the links are the bar's again: a menu left
        open would come back open when the window narrows. And a page shown
        again from the browser's memory (Back) comes back shut."""
        self.device(*self.PHONE)
        self.open()
        self.open_the_menu()
        self.device(1280)
        self.wait_for(lambda: self.expanded() == "false")
        self.device(*self.PHONE)
        self.assertEqual(self.folded()["linksDrawn"], 0)
        self.open_the_menu()
        self.script("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));")
        self.assertEqual(self.expanded(), "false")
        self.assertEqual(self.folded()["linksDrawn"], 0)

    def test_a_link_of_the_menu_opens_its_page_with_the_menu_shut(self):
        self.device(*self.PHONE)
        self.open()
        self.open_the_menu()
        bank = reverse("bank:bank_home")
        self.tap(f'.topbar nav a[href="{bank}"]')
        self.wait_for(lambda: self.script("return location.pathname + document.readyState") == bank + "complete")
        m = self.folded()
        self.assertEqual(m["expanded"], "false")
        self.assertEqual(m["linksDrawn"], 0)
        self.assertIsNotNone(m["toggle"])
        self.assertEqual(m["section"]["text"], "Banque")

    def test_the_dot_follows_the_badges_whoever_writes_them(self):
        """The dot on « Menu » is read off the badges themselves
        (`.topbar:has(nav .badge)`), so it follows every writer of them.
        Here the till products to link are all that waits, and ui.js's
        `to-link-count` - a till product linked in place - rewrites their
        badge: 0 takes it away, and the dot with it; 7 draws it again, as
        nodes in the shape base.html draws it, never as markup read from
        the event."""
        from inventory.models import Product
        from invoices.models import Invoice

        with bound_tenant(self.bar):
            Invoice.objects.all().delete()
            Product.objects.all().delete()
        self.device(*self.PHONE)
        self.open(reverse("recipes:pos_product_list"))
        badges = "return Array.from(document.querySelectorAll('.topbar nav .badge')).map(function (b) { return b.parentNode.id; });"
        self.assertEqual(self.script(badges), ["nav-count-recettes"])
        self.assertTrue(self.folded()["dot"])
        count = "document.dispatchEvent(new CustomEvent('to-link-count', {detail: {value: arguments[0]}}));"
        drawn = "return document.getElementById('nav-count-recettes').innerHTML;"

        self.script(count, 0)
        self.wait_for(lambda: not self.folded()["dot"])
        self.assertEqual(self.script(drawn), "")
        self.assertFalse(self.folded()["dotWords"])

        self.script(count, 7)
        self.wait_for(lambda: self.folded()["dot"])
        self.assertEqual(self.script(drawn), ' <span class="badge">7</span>')
        self.assertTrue(self.folded()["dotWords"])

        self.script(count, "<b>9</b>")
        self.assertEqual(self.script(drawn), ' <span class="badge">&lt;b&gt;9&lt;/b&gt;</span>')
        self.assertEqual(self.script("return document.querySelectorAll('#nav-count-recettes b').length;"), 0)

    def test_after_a_back_through_the_achats_tabs_one_tap_opens_it(self):
        """Achats' tabs are boosted, and a Back to one (htmx keeps no page
        here) swaps the whole body in again, bar included: the listeners are
        on the document, so the new bar needs nothing, and one tap is one
        opening. This does not exercise topbar.js's once-guard - in the head,
        the swap never runs it again - only that a swapped bar works
        (test_loaded_twice_one_tap_still_opens_it runs it twice)."""
        self.device(*self.PHONE)
        self.open()
        self.script("window.pageMark = 'toujours là'; document.querySelector('.topbar').dataset.drawnFirst = '1';")
        tab = self.script("var tab = document.querySelectorAll('nav.tabs a')[1]; tab.click(); return tab.pathname;")
        self.wait_for(lambda: self.script("return location.pathname;") == tab)
        time.sleep(0.5)
        self.driver.back()
        self.wait_for(lambda: self.script("return location.pathname;") == reverse("invoices:invoice_list"))
        # Drawn again by htmx in the same page - not a page loaded anew,
        # which would prove nothing about a bar swapped in.
        self.wait_for(lambda: not self.script("return !!document.querySelector('.topbar[data-drawn-first]');"))
        self.assertEqual(self.script("return window.pageMark;"), "toujours là")
        time.sleep(0.5)
        self.open_the_menu()
        # Still open once nothing moves: a listener there twice would have
        # shut it on the same tap.
        self.assertEqual(self.still("return document.querySelector('[data-topbar-toggle]').getAttribute('aria-expanded');"), "true")
        self.assertTrue(all(link["reachable"] for link in self.script(OPENED)["links"]))

    def test_loaded_twice_one_tap_still_opens_it(self):
        """topbar.js sets itself up once: run a second time, every listener
        would be there twice, and one tap on « Menu » would open the menu
        and shut it again at once. The head is never swapped by htmx, but a
        page may one day load it twice (a template including it again, a
        script tag copied into a partial): the second run must add nothing.
        Checked here by loading the same file again."""
        self.device(*self.PHONE)
        self.open()
        self.script(
            "window.loadedTwice = false;"
            " var again = document.createElement('script');"
            " again.src = document.querySelector('script[src*=\"js/topbar.js\"]').src + '&encore=1';"
            " again.onload = function () { window.loadedTwice = true; };"
            " document.head.appendChild(again);"
        )
        self.wait_for(lambda: self.script("return window.loadedTwice;"))
        self.assertEqual(self.expanded(), "false", "« Menu » drawn shut")
        self.tap("[data-topbar-toggle]")
        state = "return document.querySelector('[data-topbar-toggle]').getAttribute('aria-expanded');"
        self.assertEqual(self.still(state), "true", "one tap opened the menu and shut it again")
        self.tap("[data-topbar-toggle]")
        self.assertEqual(self.still(state), "false")

    def test_logging_out_through_the_opened_menu(self):
        """« Se déconnecter » is in the menu: a finger opens it and taps it
        (the other logout tests click it through script, which reaches a
        button nobody can see)."""
        self.device(*self.PHONE)
        self.open()
        self.open_the_menu()
        self.assertTrue(self.script(OPENED)["logout"]["reachable"])
        self.tap(".topbar-logout button")
        self.wait_for(lambda: self.script("return location.pathname") == reverse("accounts:login"))
        self.assertIn("Vous êtes déconnecté.", self.driver.page_source)
