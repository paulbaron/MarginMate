"""What the notifications' scripts promise, read on the files themselves
(no browser runs them in the fast suite): the service worker shows every
push and caches nothing, the pages' sync never asks for the permission nor
creates a device, « Activer » subscribes inside its click before anything
else, the login keeps the fragment. Their ES5-and-no-markup rules are in
tests/test_ui.py with every other script's."""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

ROOT = Path(settings.BASE_DIR)
SERVICE_WORKER = ROOT / "notifications" / "service_worker.js"
PUSH_SYNC = ROOT / "static" / "js" / "push_sync.js"
NOTIFICATIONS = ROOT / "static" / "js" / "notifications.js"
LOGIN_NEXT = ROOT / "static" / "js" / "login_next.js"
HOME_TEMPLATE = ROOT / "notifications" / "templates" / "notifications" / "home.html"


def code_of(path: Path) -> str:
    """The script without its comments: what a rule says, never what a
    comment says about it."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


class ServiceWorkerTests(SimpleTestCase):
    def setUp(self):
        self.code = code_of(SERVICE_WORKER)

    def test_every_push_shows_a_notification(self):
        push = self.code[self.code.index('addEventListener("push"') :]
        push = push[: push.index("});")]
        self.assertIn("event.waitUntil(self.registration.showNotification(", push)
        # A parse failure still shows « MarginMate ».
        self.assertIn('var FALLBACK_TITLE = "MarginMate";', self.code)
        self.assertIn("catch (error)", self.code)

    def test_it_reads_the_declarative_shape_and_a_plain_one(self):
        for name in ("data.notification", "notification.navigate", "notification.url", "notification.tag"):
            with self.subTest(name=name):
                self.assertIn(name, self.code)

    def test_no_fetch_handler_and_no_cache(self):
        self.assertNotIn('addEventListener("fetch"', self.code)
        self.assertNotIn("caches", self.code)

    def test_it_takes_over_at_once(self):
        self.assertIn("self.skipWaiting()", self.code)
        self.assertIn("event.waitUntil(self.clients.claim())", self.code)

    def test_a_click_opens_this_site_s_page_once(self):
        self.assertIn("parsed.pathname + parsed.search + parsed.hash, self.location.origin", self.code)
        self.assertIn('matchAll({ type: "window", includeUncontrolled: true })', self.code)
        self.assertEqual(self.code.count("self.clients.openWindow("), 1)
        self.assertIn("client.navigate(target)", self.code)

    def test_a_replaced_subscription_is_subscribed_again_and_nothing_else(self):
        change = self.code[self.code.index('addEventListener("pushsubscriptionchange"') :]
        self.assertIn("self.registration.pushManager.subscribe(event.oldSubscription.options)", change)
        self.assertNotIn("fetch(", change)

    def test_its_icons_exist(self):
        for path in re.findall(r'"(/static/icons/[^"]+)"', self.code):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path.lstrip("/")).is_file())

    def test_nothing_reads_as_template_syntax(self):
        """Served as it is, and swept by the pages' smoke tests."""
        text = SERVICE_WORKER.read_text(encoding="utf-8")
        for marker in ("{#", "#}", "{%", "%}", "{{"):
            self.assertNotIn(marker, text)


class PushSyncTests(SimpleTestCase):
    def setUp(self):
        self.code = code_of(PUSH_SYNC)

    def test_it_never_asks_for_the_permission_nor_creates_a_device(self):
        self.assertNotIn("requestPermission", self.code)
        self.assertNotIn(reverse("notifications:subscribe"), self.code)
        self.assertNotIn("inscrire", self.code)
        self.assertIn('Notification.permission !== "granted"', self.code)

    def test_it_registers_the_root_service_worker(self):
        self.assertIn('navigator.serviceWorker.register("/sw.js", { scope: "/" })', self.code)

    def test_its_keys_are_drafts_under_the_espace(self):
        self.assertIn('var SYNC_KEY = "marginmate:" + TENANT_SCOPE + "push:synchro";', self.code)
        self.assertIn('var RENEW_KEY = "marginmate:" + TENANT_SCOPE + "push:renouvele";', self.code)
        self.assertIn("var SYNC_EVERY = 12 * 3600 * 1000;", self.code)
        self.assertIn("var RENEW_EVERY = 3600 * 1000;", self.code)

    def test_it_keeps_a_hash_never_the_endpoint(self):
        self.assertIn('crypto.subtle.digest("SHA-256"', self.code)
        stored = re.findall(r"localStorage\.setItem\((\w+), ([^;]+)\);", self.code)
        self.assertEqual(
            stored,
            [("SYNC_KEY", "JSON.stringify({ at: Date.now(), sha: sha })"), ("RENEW_KEY", "String(Date.now())")],
        )

    def test_a_login_page_is_no_answer(self):
        self.assertIn("response.redirected", self.code)
        self.assertIn('type.indexOf("application/json") !== 0', self.code)

    def test_the_addresses_come_from_the_page(self):
        self.assertIn('getAttribute("data-sync-url")', self.code)
        self.assertIn('getAttribute("data-key-url")', self.code)


class NotificationsScriptTests(SimpleTestCase):
    def setUp(self):
        self.code = code_of(NOTIFICATIONS)

    def test_every_state_the_page_draws_is_one_the_script_shows(self):
        drawn = set(re.findall(r'data-device-state="([a-z-]+)"', HOME_TEMPLATE.read_text(encoding="utf-8")))
        shown = set(re.findall(r'show\("([a-z-]+)"\)', self.code))
        shown |= set(re.findall(r'"(denied-ios|denied)"', self.code))
        self.assertEqual(drawn - {"nojs"}, shown)

    def test_activer_subscribes_first_in_its_click(self):
        click = self.code[self.code.index('activate.addEventListener("click"') :]
        click = click[: click.index("        });\n")]
        self.assertLess(click.index("pushManager.subscribe("), click.index("register(subscription)"))
        self.assertNotIn("fetch(", click[: click.index("pushManager.subscribe(")])

    def test_it_keeps_nothing_in_the_browser(self):
        self.assertNotIn("localStorage", self.code)
        self.assertNotIn("sessionStorage", self.code)

    def test_the_key_comes_from_the_island(self):
        self.assertIn('document.getElementById("push-config")', self.code)
        self.assertIn("JSON.parse(island.textContent)", self.code)

    def test_the_path_field_follows_the_page_choice(self):
        self.assertIn("select[data-page-select]", self.code)
        self.assertIn('select.value !== "autre"', self.code)

    def said(self) -> list[str]:
        """What every `say(…)` of « Cet appareil » is handed."""
        found, start = [], 0
        while (start := self.code.find("say(", start)) != -1:
            start += len("say(")
            depth, end = 1, start
            while depth:
                depth += {"(": 1, ")": -1}.get(self.code[end], 0)
                end += 1
            found.append(self.code[start : end - 1].strip())
        return [argument for argument in found if argument != "text"]  # the function's own definition

    def test_the_screen_says_the_server_s_french_or_a_fixed_sentence(self):
        # A rejection's own message is English or technical: readJson's
        # « not an answer » (a login redirect, a 500 page), the browser's
        # « Failed to fetch » / « Load failed ».
        self.assertNotIn("error.message", self.code)
        allowed = (
            r'"[^"]*"',  # a fixed sentence
            r"[A-Z][A-Z_]+",  # a constant holding one
            r"\(error && error\.said\) \|\| NOT_REGISTERED",  # the server's JSON refusal
            r'Notification\.permission === "granted" \? [A-Z][A-Z_]+ : [A-Z][A-Z_]+',
            r'\(result\.data && \(result\.data\.message \|\| result\.data\.error\)\) \|\| "[^"]*"',
        )
        for argument in self.said():
            with self.subTest(argument=argument):
                self.assertTrue(any(re.fullmatch(pattern, argument) for pattern in allowed), argument)

    def test_only_a_string_the_server_answered_is_shown(self):
        register = self.code[self.code.index("function register(") :]
        register = register[: register.index("\n        }\n")]
        self.assertIn('typeof result.data.error === "string"', register)
        self.assertNotIn("new Error((result.data", register)

    def test_a_subscription_refused_is_not_said_activated(self):
        click = self.code[self.code.index('activate.addEventListener("click"') :]
        rejected = click[click.index("}, function () {") : click.index("}).then(function () {")]
        self.assertNotIn("NOT_REGISTERED", rejected)
        self.assertNotIn("Activé dans le navigateur", rejected)
        self.assertIn('Notification.permission === "granted"', rejected)
        self.assertIn("SUBSCRIBE_FAILED", rejected)
        self.assertIn("NOT_ALLOWED", rejected)
        for name, sentence in (
            ("NOT_REGISTERED", "Activé dans le navigateur mais pas enregistré : réessayez."),
            ("SUBSCRIBE_FAILED", "Inscription auprès du service de notification impossible : réessayez."),
            ("NOT_ALLOWED", "Notifications non autorisées : touchez Activer et acceptez la demande."),
        ):
            with self.subTest(name=name):
                self.assertIn(f'var {name} = "{sentence}";', self.code)

    def test_activated_but_not_registered_only_once_subscribed(self):
        # The browser holds a subscription: the inscrire POST, or the load's
        # sync, did not go through.
        click = self.code[self.code.index('activate.addEventListener("click"') :]
        accepted = click[click.index("subscribing.then(function (subscription) {") : click.index("}, function () {")]
        self.assertIn("register(subscription).catch(", accepted)
        self.assertIn("NOT_REGISTERED", accepted)


class LoginNextTests(SimpleTestCase):
    def test_it_appends_the_fragment_to_next_once(self):
        code = code_of(LOGIN_NEXT)
        self.assertIn("window.location.hash", code)
        self.assertIn("input[type=hidden][name=next]", code)
        self.assertIn('next.indexOf("#") === -1', code)
