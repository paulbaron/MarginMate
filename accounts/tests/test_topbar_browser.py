"""The topbar of a logged-in page, in a real (headless) Chrome: the bar's
name and « Se déconnecter » beside the links must not make the bar taller
than the room the page leaves above what it scrolls to (--topbar-room,
static/css/marginmate.css; invoices.tests.test_changes_to_see.
TopbarRoomInBrowserTests measures where the pages scroll to, logged in too).

Measured at the widths where the bar changes shape, with a badge on each
counting link (they are what makes the links wrap) and a bar name of a
common length. Under 860 px the name and the button sit on the brand's row,
so they add no row; above it they take room from the links.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.scrapers import website
from recipes.models import PosProduct
from tests.factories import make_invoice, make_product, make_supplier

MEASURE = """
const bar = document.querySelector('.topbar').getBoundingClientRect();
const room = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop);
const brand = document.querySelector('.brand').getBoundingClientRect();
const account = document.querySelector('.topbar-account');
const button = account.querySelector('button').getBoundingClientRect();
const name = account.querySelector('.topbar-espace');
return {
    height: bar.height, room: room,
    brandTop: brand.top, brandBottom: brand.bottom,
    buttonTop: button.top, buttonBottom: button.bottom, buttonLeft: button.left, buttonRight: button.right,
    buttonWidth: button.width, viewport: document.documentElement.clientWidth,
    nameShown: getComputedStyle(name).display !== 'none' ? name.getBoundingClientRect().width : 0,
};
"""


@tag("browser")
class TopbarOfAnEspaceInBrowserTests(TenancyTestCase, StaticLiveServerTestCase):
    WIDTHS = (1280, 1100, 1000, 960, 900, 861, 860, 768, 600, 465, 450, 375, 334, 320, 310, 300, 280)
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
        self.client.force_login(self.user)
        self.driver.get(self.live_server_url + reverse("accounts:login"))
        self.driver.add_cookie({"name": "sessionid", "value": self.client.cookies["sessionid"].value, "path": "/"})

    def viewport(self, width):
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": self.HEIGHT, "deviceScaleFactor": 1, "mobile": False},
        )

    def test_the_bar_s_name_and_the_logout_fit_in_the_room(self):
        """A name of common length, then one past the 12rem the name may
        take (cut with « … »), then the same for a superuser, whose links
        also carry « Admin »: the room must hold the bar every way."""
        page = self.live_server_url + reverse("invoices:invoice_list")
        long_name = "Le Comptoir des Essais et de la Dégustation du Quartier"
        rows, problems = [], []
        for name, superuser in (("Le Comptoir des Essais", False), (long_name, False), (long_name, True)):
            type(self.bar).objects.filter(pk=self.bar.pk).update(name=name)
            type(self.user).objects.filter(pk=self.user.pk).update(is_superuser=superuser, is_staff=superuser)
            rows.append(f"{name}{' (superuser)' if superuser else ''}")
            for width in self.WIDTHS:
                self.viewport(width)
                self.driver.get(page)
                m = self.driver.execute_script(MEASURE)
                rows.append(f"{width:>5} px: bar {m['height']:.0f} / room {m['room']:.0f}, name {m['nameShown']:.0f} px")
                where = f"{name[:22]}…, {width} px"
                if m["height"] > m["room"]:
                    problems.append(f"{where}: the bar is {m['height']:.0f} px tall, the room {m['room']:.0f}")
                if m["buttonWidth"] <= 0 or m["buttonRight"] > m["viewport"] or m["buttonLeft"] < 0:
                    problems.append(f"{where}: « Se déconnecter » is not on the screen")
                if width <= 860 and not (m["buttonTop"] < m["brandBottom"] and m["buttonBottom"] > m["brandTop"]):
                    problems.append(f"{where}: « Se déconnecter » is not on the brand's row")
                if width >= 320 and m["nameShown"] < 50:
                    problems.append(f"{where}: the bar's name is {m['nameShown']:.0f} px wide")
        self.assertEqual(problems, [], "\n".join(rows))

    def test_the_logout_button_logs_out(self):
        self.viewport(375)
        self.driver.get(self.live_server_url + reverse("invoices:invoice_list"))
        self.driver.execute_script("document.querySelector('.topbar-logout button').click()")
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(self.driver, 10).until(lambda driver: driver.execute_script("return location.pathname") == "/connexion/")
        self.assertIn("Vous êtes déconnecté.", self.driver.page_source)
