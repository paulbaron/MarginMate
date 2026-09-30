"""What every answer of the application carries, with DEBUG off as in
production (the test settings'): the Content-Security-Policy and the other
security headers (config/security.py; security audit DEPLOY-6), French error
pages with nothing internal on them (ANON-2, DEPLOY-1, LB-1), and /static/
served by WhiteNoise from what collectstatic gathered.

`PolicyInBrowserTests` (tag "browser", the cached chromedriver) opens the
pages in Chrome under the policy: nothing refused, and htmx's trigger
filters - the reason for 'unsafe-eval' - still run."""

import re
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
    tag,
)
from django.urls import include, path, reverse

from accounts import paths
from config import security
from staff import public_views
from tests.runner import log_in_the_browser
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax


def _breaks(request):
    raise RuntimeError("panne interne dans C:/chemin/secret/settings.py")


def _forbids(request):
    raise PermissionDenied("raison interne du refus")


#: This module is the URLconf of the error pages' tests: two views that fail
#: on purpose, and the application.
urlpatterns = [
    path("essai-panne/", login_not_required(_breaks)),
    path("essai-interdit/", login_not_required(_forbids)),
    path("", include("config.urls")),
]

#: What an error page must never show: a trace, a setting, a path, the route
#: map, the technical reason of a refusal.
INTERNAL = (
    "Traceback",
    "RuntimeError",
    "settings",
    "DEBUG",
    "Django",
    "ALLOWED_HOSTS",
    "DisallowedHost",
    "CSRF",
    "Referer",
    "chemin/secret",
    "raison interne",
    "donnees/",
    "banque/",
    "inscription/",
    str(settings.BASE_DIR),
    str(settings.TENANTS_ROOT),
)


class SecurityHeadersTests(TestCase):
    def assertHardened(self, response, policy=security.APP_POLICY):
        self.assertEqual(response["Content-Security-Policy"], policy)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")

    def test_the_login_page(self):
        response = Client().get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertHardened(response)
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_a_bar_s_page(self):
        response = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(response.status_code, 200)
        self.assertHardened(response)
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_the_policy_says_what_the_pages_may_do(self):
        policy = security.APP_POLICY
        for rule in (
            "default-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "img-src 'self' data: blob:",
        ):
            self.assertIn(rule, policy)
        # Nothing from another origin: no host, no scheme but data: and blob:.
        self.assertNotRegex(policy, r"https?:|\*")

    def test_the_employee_s_pages_keep_their_stricter_policy(self):
        response = Client().get(reverse("staff:sign", args=["lien-qui-n-existe-pas"]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Security-Policy"], public_views.CONTENT_SECURITY_POLICY)

    def test_a_stored_pdf_is_left_to_the_browser_s_viewer(self):
        """The invoice's PDF is shown in a frame of this site: no policy on
        the PDF itself (the browser's viewer draws it), SAMEORIGIN kept."""
        (paths.media_root() / "facture-essai.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        response = self.client.get(reverse("accounts:media", args=["facture-essai.pdf"]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Content-Security-Policy", response)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_a_downloaded_file_keeps_its_sandbox(self):
        (paths.media_root() / "facture-essai.xml").write_bytes(b"<Invoice/>")
        response = self.client.get(reverse("accounts:media", args=["facture-essai.xml"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Security-Policy"], "sandbox")


class ContentSecurityPolicyMiddlewareTests(SimpleTestCase):
    def answer(self, response):
        return security.ContentSecurityPolicyMiddleware(lambda request: response)(RequestFactory().get("/"))

    def test_an_html_page_gets_the_policy(self):
        self.assertEqual(self.answer(HttpResponse("<p>page</p>"))["Content-Security-Policy"], security.APP_POLICY)

    def test_a_page_framed_by_this_site_may_be_framed_by_this_site_only(self):
        response = HttpResponse("<p>page</p>")
        response["X-Frame-Options"] = "SAMEORIGIN"
        policy = self.answer(response)["Content-Security-Policy"]
        self.assertEqual(policy, security.SAME_ORIGIN_POLICY)
        self.assertTrue(policy.endswith("frame-ancestors 'self'"))

    def test_a_page_exempt_from_framing_rules_gets_none(self):
        response = HttpResponse("<p>page</p>")
        response.xframe_options_exempt = True
        self.assertNotIn("frame-ancestors", self.answer(response)["Content-Security-Policy"])

    def test_a_policy_of_its_own_is_kept(self):
        response = HttpResponse("<p>page</p>")
        response["Content-Security-Policy"] = "default-src 'none'"
        self.assertEqual(self.answer(response)["Content-Security-Policy"], "default-src 'none'")

    def test_anything_but_html_gets_none(self):
        self.assertNotIn("Content-Security-Policy", self.answer(JsonResponse({"a": 1})))
        pdf = FileResponse(iter([b"%PDF-1.4"]), content_type="application/pdf")
        self.assertNotIn("Content-Security-Policy", self.answer(pdf))


def _project_templates():
    base = Path(settings.BASE_DIR)
    found = [*base.glob("*/templates/**/*.html"), *(base / "templates").glob("**/*.html")]
    return sorted({template for template in found if ".venv" not in template.parts})


def _occurrences(pattern) -> list[str]:
    """« file:line » of every match of `pattern` in the project's templates."""
    base = Path(settings.BASE_DIR)
    places = []
    for template in _project_templates():
        text = template.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            places.append(f"{template.relative_to(base).as_posix()}:{line}")
    return places


def _directive(name: str) -> str:
    return next(rule for rule in security.APP_POLICY.split("; ") if rule.startswith(name + " "))


#: A <script> that runs inline: no src, not a JSON island.
INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)(?![^>]*\btype=[\"']application/(?:ld\+)?json)[^>]*>", re.IGNORECASE)
#: onclick=, onsubmit=… on an element.
EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=\s*[\"']", re.IGNORECASE)
#: What htmx turns into code with Function(): a trigger's [filter], hx-on,
#: js: values.
HTMX_EVAL = re.compile(r"hx-trigger=\"[^\"]*\[|hx-on[:-]|hx-vals=[\"']js:|hx-vars=", re.IGNORECASE)
INLINE_STYLE = re.compile(r"\sstyle=[\"']|<style\b", re.IGNORECASE)


class PolicyFollowsTheTemplatesTests(SimpleTestCase):
    """'unsafe-inline' and 'unsafe-eval' are in the policy for a reason the
    templates give (config/security.py lists them): each test fails the day
    the reason is gone, so that the policy tightens with the templates."""

    def assertAllowedExactlyWhileNeeded(self, directive, keyword, places, what):
        rule = _directive(directive)
        if places:
            self.assertIn(keyword, rule, f"{what} : {', '.join(places)}")
        else:
            self.assertNotIn(
                keyword, rule, f"Plus aucun(e) {what} : retirez {keyword} de {directive} (config/security.py)."
            )

    def test_inline_scripts(self):
        places = _occurrences(INLINE_SCRIPT) + _occurrences(EVENT_HANDLER)
        self.assertAllowedExactlyWhileNeeded("script-src", "'unsafe-inline'", places, "script en ligne")

    def test_code_htmx_makes_from_strings(self):
        places = _occurrences(HTMX_EVAL)
        self.assertAllowedExactlyWhileNeeded("script-src", "'unsafe-eval'", places, "filtre htmx évalué")

    def test_inline_styles(self):
        places = _occurrences(INLINE_STYLE)
        self.assertAllowedExactlyWhileNeeded("style-src", "'unsafe-inline'", places, "style en ligne")

    def test_what_the_patterns_find(self):
        island = '<script id="d" type="application/json">{"a": 1}</script>'
        self.assertIsNone(INLINE_SCRIPT.search(island))
        self.assertIsNotNone(INLINE_SCRIPT.search("<script>var a = 1;</script>"))
        self.assertIsNone(INLINE_SCRIPT.search('<script src="/static/js/ui.js" defer></script>'))
        self.assertIsNotNone(EVENT_HANDLER.search('<form onsubmit="return confirm(\'Supprimer ?\');">'))
        self.assertIsNone(EVENT_HANDLER.search('<div data-once="1">'))
        # The Tickets' status (invoices/_receipt_batch_status.html).
        self.assertIsNotNone(HTMX_EVAL.search('<div hx-get="/x" hx-trigger="every 1s [!shopChoiceInUse()]">'))
        self.assertIsNone(HTMX_EVAL.search('<div hx-get="/x" hx-trigger="every 1s">'))
        self.assertIsNotNone(INLINE_STYLE.search('<p style="margin: 0">'))


@override_settings(ROOT_URLCONF=__name__)
class ErrorPagesTests(TestCase):
    """DEBUG off: Django's plain pages were English, and with DEBUG on its
    technical ones showed the settings and the route map to anybody."""

    def assertPlainFrench(self, response, status, heading):
        self.assertEqual(response.status_code, status)
        page = response.content.decode()
        self.assertIn(f"<h1>{heading}</h1>", page)
        self.assertIn('<html lang="fr">', page)
        self.assertIn("Revenir à l'accueil", page)
        for word in INTERNAL:
            self.assertNotIn(word, page)
        self.assertNotIn("<nav", page)
        assertNoUnrenderedTemplateSyntax(self, response, heading)
        return page

    def test_404_for_a_visitor(self):
        response = Client().get("/une-adresse-inventee/")
        page = self.assertPlainFrench(response, 404, "Page introuvable")
        # The address asked for is not written back.
        self.assertNotIn("une-adresse-inventee", page)
        self.assertEqual(response["Content-Security-Policy"], security.APP_POLICY)

    def test_404_for_a_bar(self):
        response = self.client.get("/une-adresse-inventee/")
        self.assertPlainFrench(response, 404, "Page introuvable")

    def test_no_media_route(self):
        self.assertPlainFrench(Client().get("/media/invoices/facture.pdf"), 404, "Page introuvable")

    def test_500(self):
        response = Client(raise_request_exception=False).get("/essai-panne/")
        self.assertPlainFrench(response, 500, "Erreur du serveur")

    def test_403(self):
        self.assertPlainFrench(Client().get("/essai-interdit/"), 403, "Accès refusé")

    def test_400_a_forged_host(self):
        response = Client(HTTP_HOST="ailleurs.example").get(reverse("accounts:login"))
        page = self.assertPlainFrench(response, 400, "Demande incorrecte")
        self.assertNotIn("ailleurs", page)

    def test_a_refused_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("accounts:login"), {"username": "alpha@example.invalid", "password": "x"})
        self.assertPlainFrench(response, 403, "La page a expiré")

    def test_the_server_error_page_renders_with_nothing_at_all(self):
        """Django renders 500.html with no request and no context (a failure
        may come from anywhere)."""
        page = render_to_string("500.html")
        self.assertIn("<h1>Erreur du serveur</h1>", page)


class StaticFilesTests(TestCase):
    """Production: WhiteNoise serves STATIC_ROOT (what collectstatic gathered
    at `serve`'s start), to anybody - the login page needs its stylesheet."""

    def test_whitenoise_serves_the_collected_files_to_anybody(self):
        root = Path(tempfile.mkdtemp(prefix="marginmate-tests-static-"))
        (root / "css").mkdir()
        (root / "css" / "essai.css").write_text("body { color: black; }", encoding="utf-8")
        with override_settings(STATIC_ROOT=root, WHITENOISE_AUTOREFRESH=False, WHITENOISE_USE_FINDERS=False):
            response = Client().get("/static/css/essai.css")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"body { color: black; }")
            self.assertTrue(response["Content-Type"].startswith("text/css"))
            # Only what was collected: nothing else of the code's folders.
            self.assertEqual(Client().get("/static/js/ui.js").status_code, 404)


#: A trigger filter, as invoices/_receipt_batch_status.html has one, put in
#: the page and processed by htmx from the page's own event loop: code run
#: inside a WebDriver call is exempt from the page's policy.
FILTERED_TRIGGER = """
window.filterRuns = 0;
window.filterErrors = [];
window.filterCheck = function () { window.filterRuns += 1; return false; };
document.body.addEventListener('htmx:syntax:error', function () { window.filterErrors.push('syntax'); });
var box = document.createElement('div');
box.setAttribute('hx-get', '/');
box.setAttribute('hx-trigger', 'every 200ms [filterCheck()]');
document.body.appendChild(box);
setTimeout(function () { htmx.process(box); }, 0);
"""


@tag("browser")
class PolicyInBrowserTests(StaticLiveServerTestCase):
    """The pages in Chrome, under the application's policy."""

    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager

            options = webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            cls.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def refused(self) -> list[str]:
        return [
            entry["message"] for entry in self.driver.get_log("browser") if "Content Security Policy" in entry["message"]
        ]

    def test_the_pages_load_with_nothing_refused(self):
        self.driver.get(self.live_server_url + reverse("accounts:login"))
        self.assertEqual(self.refused(), [])
        log_in_the_browser(self.driver, self.live_server_url)
        for name in (
            "inventory:stock_list",
            "inventory:stock_take_list",
            "inventory:stock_take_create",
            "invoices:invoice_list",
            "recipes:recipe_list",
            "bank:bank_home",
            "margins:margins_home",
            "staff:home",
            "returnables:home",
            "transfer:data_home",
        ):
            with self.subTest(page=name):
                self.driver.get(self.live_server_url + reverse(name))
                self.assertEqual(self.refused(), [])

    def test_an_htmx_trigger_filter_still_runs(self):
        """'unsafe-eval' is in script-src for this: htmx makes a trigger's
        filter with Function(). Refused, the filter is dropped - the Tickets'
        status then replaced the shop being chosen every second."""
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.get(self.live_server_url + reverse("inventory:stock_list"))
        self.driver.execute_script(FILTERED_TRIGGER)
        time.sleep(1)
        runs, errors = self.driver.execute_script("return [window.filterRuns, window.filterErrors];")
        self.assertGreater(runs, 0)
        self.assertEqual(errors, [])
        self.assertEqual(self.refused(), [])
