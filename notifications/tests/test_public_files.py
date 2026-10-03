"""What a browser fetches without a session: /sw.js, /manifest.webmanifest,
the icons - and the head tags pointing at them, the login page's included.
A service worker's script must never be the login page (a redirected script
is an error), and a manifest is fetched without credentials."""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.checks import run_checks
from django.test import Client, SimpleTestCase, TestCase
from django.urls import resolve, reverse
from PIL import Image

from accounts.models import Membership
from notifications import views
from tests.runner import TEST_TENANT_PK

SW = "/sw.js"
MANIFEST = "/manifest.webmanifest"
ICONS = Path(settings.BASE_DIR) / "static" / "icons"
ICON_SIZES = {
    "icon-192.png": (192, 192),
    "icon-512.png": (512, 512),
    "icon-maskable-512.png": (512, 512),
    "apple-touch-icon.png": (180, 180),
    "badge-96.png": (96, 96),
    "favicon-32.png": (32, 32),
}
HEAD_TAGS = (
    '<link rel="manifest" href="/manifest.webmanifest">',
    '<meta name="theme-color" content="#1a1a21">',
    '<link rel="icon" href="/static/icons/favicon-32.png">',
    '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">',
    '<meta name="apple-mobile-web-app-title" content="MarginMate">',
    '<meta name="apple-mobile-web-app-status-bar-style" content="black">',
)


class AddressTests(SimpleTestCase):
    def test_at_the_root(self):
        self.assertEqual(reverse("notifications:service_worker"), SW)
        self.assertEqual(reverse("notifications:manifest"), MANIFEST)

    def test_both_are_public_and_nothing_else_of_the_app_is(self):
        self.assertFalse(resolve(SW).func.login_required)
        self.assertFalse(resolve(MANIFEST).func.login_required)
        for name in ("home", "reminders", "events", "key", "subscribe", "sync", "test"):
            with self.subTest(name=name):
                self.assertTrue(getattr(resolve(reverse(f"notifications:{name}")).func, "login_required", True))

    def test_the_app_is_included_once(self):
        ids = [message.id for message in run_checks(tags=["urls"])]
        self.assertNotIn("urls.W005", ids)


class ServiceWorkerTests(TestCase):
    def test_served_anonymously_as_javascript_never_cached(self):
        response = Client().get(SW)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(
            response.content.decode(), (Path(views.__file__).with_name("service_worker.js")).read_text("utf-8")
        )
        # Unbound: no espace, no session, no page policy.
        self.assertIsNone(getattr(response.wsgi_request, "tenant", None))
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn("Content-Security-Policy", response)

    def test_a_logged_in_browser_gets_the_same(self):
        response = self.client.get(SW)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-cache")

    def test_only_read(self):
        self.assertEqual(Client().post(SW).status_code, 405)


class ManifestTests(TestCase):
    def test_what_makes_the_home_screen_app(self):
        response = Client().get(MANIFEST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        self.assertEqual(response["Cache-Control"], "max-age=3600")
        manifest = json.loads(response.content)
        self.assertEqual(
            {key: manifest[key] for key in manifest if key != "icons"},
            {
                "id": "/",
                "name": "MarginMate",
                "short_name": "MarginMate",
                "lang": "fr",
                "dir": "ltr",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#131318",
                "theme_color": "#1a1a21",
            },
        )
        self.assertEqual(
            manifest["icons"],
            [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {
                    "src": "/static/icons/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        )
        self.assertIsNone(getattr(response.wsgi_request, "tenant", None))

    def test_every_icon_named_exists(self):
        manifest = json.loads(Client().get(MANIFEST).content)
        for icon in manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                self.assertTrue((ICONS / icon["src"].rsplit("/", 1)[1]).is_file())


class IconTests(SimpleTestCase):
    def test_each_icon_its_size_and_small(self):
        for name, size in ICON_SIZES.items():
            with self.subTest(icon=name):
                path = ICONS / name
                self.assertLess(path.stat().st_size, 50 * 1024)
                with Image.open(path) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, size)

    def test_the_apple_touch_icon_is_opaque(self):
        with Image.open(ICONS / "apple-touch-icon.png") as image:
            self.assertEqual(image.mode, "RGB")

    def test_the_badge_is_a_white_glyph_on_transparent(self):
        with Image.open(ICONS / "badge-96.png") as image:
            rgba = image.convert("RGBA")
            self.assertEqual(rgba.getpixel((0, 0))[3], 0)
            opaque = [pixel for pixel in rgba.getdata() if pixel[3] == 255]
            self.assertTrue(opaque)
            self.assertTrue(all(pixel[:3] == (255, 255, 255) for pixel in opaque))


class HeadTagsTests(TestCase):
    def head(self, response) -> str:
        return response.content.decode().split("</head>")[0]

    def test_every_page_of_an_espace_carries_them_and_the_sync(self):
        head = self.head(self.client.get(reverse("inventory:stock_list")))
        for tag in HEAD_TAGS:
            with self.subTest(tag=tag):
                self.assertIn(tag, head)
        self.assertRegex(head, r'<script src="/static/js/push_sync\.js\?v=\d+" defer data-sync-url=')

    def test_the_login_page_carries_them_but_never_the_sync(self):
        response = Client().get(reverse("accounts:login"))
        head = self.head(response)
        for tag in HEAD_TAGS:
            with self.subTest(tag=tag):
                self.assertIn(tag, head)
        self.assertNotIn("push_sync.js", response.content.decode())
        self.assertRegex(head, r'<script src="/static/js/login_next\.js\?v=\d+" defer></script>')


class LoginKeepsTheFragmentTests(TestCase):
    """login_next.js puts the page's fragment back on `next`; the server
    then follows it, fragment included."""

    PASSWORD = "mot-de-passe-essai-tres-long"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        user = get_user_model().objects.create_user(
            username="matin@example.invalid", email="matin@example.invalid", password=self.PASSWORD
        )
        Membership.objects.create(user=user, tenant_id=TEST_TENANT_PK)

    def test_a_next_with_its_fragment_lands_on_it(self):
        response = Client().post(
            reverse("accounts:login"),
            {"username": "matin@example.invalid", "password": self.PASSWORD, "next": "/consignes/#new-pickup"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/consignes/#new-pickup")

    def test_the_login_page_draws_next_for_the_script(self):
        response = Client().get(reverse("accounts:login"), {"next": "/consignes/"})
        self.assertContains(response, '<input type="hidden" name="next" value="/consignes/">')
