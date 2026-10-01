"""What keeps a customer portal's password where it belongs (security audit
of 01/10/2026): sent only from an https login page, typed only on the site
it was typed for on « Identifiants », only into a password field, and never
left behind in the page kept for debugging, a log line or an error.

No browser and no network: the browser is a fake answering the scraper's
scripts (test_website_scraper_browser.py drives a real Chrome against a
portal served from this machine). Data invented.
"""

import os
import re
import shutil
import tempfile
import time
import types
from datetime import date
from unittest import mock

import requests
from django.test import SimpleTestCase

from accounts import paths, vault
from invoices.models import portal_host
from invoices.scrapers import website
from invoices.scrapers.website import (
    WebsiteError,
    WebsiteRecipe,
    bare_url,
    credentials,
    fetch_website_invoices,
    holds_a_secret,
    mask_secrets,
    registrable_domain,
    same_site,
)
from tests.support import NoNetworkTestCase

LOGIN = "gerant@exemple.invalid"
# A password needing every escape a page may write it with: a quote and
# angle brackets (HTML), an ampersand (HTML, a URL), an accent (JSON, a URL).
SECRET = 'Pa"ss<w>&ord-é'
# The forms it takes in a page, written out by hand - not worked out by the
# code under test, which would then only be checked against itself.
SECRET_FORMS = (
    SECRET,
    "Pa&quot;ss&lt;w&gt;&amp;ord-é",  # html.escape, quote=True
    'Pa"ss&lt;w&gt;&amp;ord-é',  # html.escape, quote=False
    'Pa\\"ss<w>&ord-\\u00e9',  # inside a JSON string
    "Pa%22ss%3Cw%3E%26ord-%C3%A9",  # percent-encoded
)
SITE = "box.exemple.invalid"
LOGIN_URL = f"https://{SITE}/login"

# A sleep that returns at once: the scraper's pauses are a person's pace.
FAST = types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None, time=time.time)


def recipe(**kwargs) -> WebsiteRecipe:
    settings = {
        "name": "Box Exemple",
        "login_url": LOGIN_URL,
        "username_env": "BOX_LOGIN",
        "password_env": "BOX_PASSWORD",
    }
    settings.update(kwargs)
    return WebsiteRecipe(**settings)


class Field:
    """An element of the fake page; what is typed into it is kept."""

    def __init__(self, kind="text", tag="input", on_typed=None):
        self.kind = kind
        self.tag_name = tag
        self.typed = []
        self.on_typed = on_typed

    def get_attribute(self, name):
        return self.kind if name == "type" else None

    def clear(self):
        pass

    def send_keys(self, *keys):
        self.typed.extend(keys)
        if self.on_typed:
            self.on_typed()


class FakeBrowser:
    """A portal as the scraper's scripts see it. `form` is what the login
    form script finds ([identifier, password, button], or the script's
    refusal); `lands_on` is where opening the login page ends up (a
    redirect); the button leads to `sends_to` and shows `next_form` (a
    two-step login), signs in, or leaves the form where it is."""

    def __init__(
        self,
        form=None,
        lands_on=None,
        sends_to=None,
        next_form=None,
        signs_in=True,
        source="<html><body></body></html>",
        error_text="",
        links=(),
        screen=(),
        screen_attributes=(),
        scrub_fails=False,
        text_fails=False,
        cookies=(),
    ):
        self.user = Field("email")
        self.password = Field("password")
        self.button = Field("submit", tag="button")
        self.form = form if form is not None else [self.user, self.password, self.button]
        self.lands_on = lands_on
        self.sends_to = sends_to
        self.next_form = next_form
        self.signs_in = signs_in
        self.source = source
        self.error_text = error_text
        self.links = list(links)
        # What the page shows (its text nodes, side by side as the page
        # draws them) and what its attributes hold (a title, an alt): what
        # the screen scrub reads and rewrites, and the page's text is.
        self.screen = list(screen)
        self.screen_attributes = list(screen_attributes)
        self.scrub_fails = scrub_fails
        self.text_fails = text_fails
        self.scrubbed_with = None
        self.cookies = list(cookies)
        self.current_url = "about:blank"
        self.events = []

    def get(self, url):
        self.current_url = self.lands_on or url

    def get_cookies(self):
        return [dict(cookie) for cookie in self.cookies]

    def scrub(self, values, mask, loose=()):
        """SCRUB_SCREEN_JS, as the page would run it: each value typed
        replaced in the texts shown - as typed, a login's spellings
        (`loose`) whatever their case -, longest first, and true only when
        no text or attribute holds one any more."""
        self.events.append("scrub")
        if self.scrub_fails:
            raise RuntimeError("javascript error")
        self.scrubbed_with = (list(values), mask, list(loose))
        wanted = [(value, 0) for value in values if value] + [(value, re.IGNORECASE) for value in loose if value]
        wanted.sort(key=lambda pair: len(pair[0]), reverse=True)
        for value, flags in wanted:
            self.screen = [re.sub(re.escape(value), lambda _found: mask, text, flags=flags) for text in self.screen]
        return not any(
            re.search(re.escape(value), text, flags)
            for value, flags in wanted
            for text in (*self.screen, *self.screen_attributes)
        )

    def execute_script(self, script, *args):
        if script is website.FIND_LOGIN_JS:
            return self.form
        if script is website.BLANK_FIELDS_JS:
            self.events.append("blank")
            return True
        if script is website.SCRUB_SCREEN_JS:
            return self.scrub(*args)
        if script is website.SCREEN_TEXT_JS:
            # The page's text as it shows: its text nodes side by side.
            self.events.append("text")
            if self.text_fails:
                raise RuntimeError("javascript error")
            return "".join(self.screen)
        if script == "return navigator.userAgent":
            return "Mozilla/5.0 (navigateur d'essai)"
        if script is website.ERROR_TEXT_JS:
            return self.error_text
        if script is website.READ_LINKS_JS:
            return self.links
        if script == "arguments[0].click();":
            self.click(args[0])
            return None
        if "input[type=password]" in script and ".some(" in script:  # the password still on screen
            return isinstance(self.form, list) and self.form[1] is not None
        if "innerText" in script:
            return ""
        return None

    def click(self, element):
        if element is not self.form[2]:
            return
        if self.sends_to:
            self.current_url = self.sends_to
        if self.next_form is not None:
            self.form, self.next_form = self.next_form, None
        elif self.signs_in:
            self.form = [None, None, None]

    def find_elements(self, by, selector):
        return [Field()]

    def save_screenshot(self, path):
        self.events.append("screenshot")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG fake")
        return True

    @property
    def page_source(self):
        self.events.append("source")
        return self.source

    def quit(self):
        pass


def quick():
    """The waits of a run against a page that answers at once."""
    patches = [mock.patch.object(website, "time", FAST)]
    for name, value in (
        ("POLL_SECONDS", 0.001),
        ("PAGE_WAIT_SECONDS", 0.2),
        ("LOGIN_WAIT_SECONDS", 0.05),
        ("SETTLE_SECONDS", 0.01),
    ):
        patches.append(mock.patch.object(website, name, value))
    return patches


class QuickCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        for patcher in quick():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logged = []


# ------------------------------------------------------------- same site


class SameSiteTests(SimpleTestCase):
    """A login's pages may move between a site's hosts (particulier.edf.fr
    to auth.edf.fr), never to another site's - and on a platform hosting
    its customers under one name (SHARED_SUFFIXES: github.io, okta.com,
    free.fr's personal pages…), every customer is a site of its own. Two
    sites read as one is the direction that sends a password where it does
    not belong."""

    def test_two_hosts_of_one_site(self):
        for host, other in (
            ("auth.edf.fr", "particulier.edf.fr"),
            ("Mobile.Free.FR.", "mobile.free.fr"),
            ("a.exemple.co.uk", "b.exemple.co.uk"),
            ("cfspart.impots.gouv.fr", "www.impots.gouv.fr"),
            ("shop.exemple.com", "www.exemple.com"),
        ):
            with self.subTest(host=host, other=other):
                self.assertTrue(same_site(host, other))

    def test_the_customers_of_a_shared_suffix_are_two_sites(self):
        for suffix in website.SHARED_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertFalse(same_site(f"fournisseur.{suffix}", f"pirate.{suffix}"))
                self.assertFalse(same_site(f"fournisseur.{suffix}", suffix))

    def test_a_customer_of_a_shared_suffix_is_one_site_with_itself(self):
        for suffix in website.SHARED_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertTrue(same_site(f"fournisseur.{suffix}", f"Fournisseur.{suffix.upper()}."))

    def test_the_label_before_a_shared_suffix_need_not_name_a_customer(self):
        """A region, a product or a sandbox stands there on several of
        them: read as the customer, every customer under it is one site."""
        for host, other in (
            ("acme.eu.auth0.com", "pirate.eu.auth0.com"),
            ("acme.lightning.force.com", "pirate.lightning.force.com"),
            ("acme--essai.sandbox.my.salesforce.com", "pirate--essai.sandbox.my.salesforce.com"),
            ("factures.s3.amazonaws.com", "pirate.s3.amazonaws.com"),
            ("espace-1a2b.francecentral-01.azurewebsites.net", "pirate-3c4d.francecentral-01.azurewebsites.net"),
            ("espace.uc.r.appspot.com", "pirate.uc.r.appspot.com"),
            ("login.acme.okta.com", "acme.okta.com"),
        ):
            with self.subTest(host=host, other=other):
                self.assertFalse(same_site(host, other))

    def test_free_and_orange_personal_pages_compare_the_exact_host(self):
        """free.fr is Free's own site and every subscriber's pages
        (« <abonné>.free.fr »), pagesperso-orange.fr every Orange
        subscriber's."""
        for host, other in (
            ("mobile.free.fr", "jean.free.fr"),
            ("mobile.free.fr", "espace.free.fr"),
            ("free.fr", "jean.free.fr"),
            ("jean.free.fr", "abonne-b.free.fr"),
            ("jean.pagesperso-orange.fr", "pirate.pagesperso-orange.fr"),
        ):
            with self.subTest(host=host, other=other):
                self.assertFalse(same_site(host, other))
        self.assertTrue(same_site("mobile.free.fr", "Mobile.Free.fr"))
        self.assertTrue(same_site("jean.free.fr", "Jean.Free.fr."))

    def test_frees_own_service_hosts_are_one_site(self):
        """The Freebox signs in at subscribe.free.fr and its invoice links
        resolve to adsl.free.fr: read as two sites, `_fetch` refused every
        invoice."""
        for host, other in (
            ("subscribe.free.fr", "adsl.free.fr"),
            ("mobile.free.fr", "subscribe.free.fr"),
            ("Subscribe.Free.fr.", "www.free.fr"),
            ("free.fr", "espaceclient.free.fr"),
            ("assistance.free.fr", "portail.free.fr"),
            ("abo.free.fr", "adsl.free.fr"),
        ):
            with self.subTest(host=host, other=other):
                self.assertTrue(same_site(host, other))

    def test_a_subscribers_pages_are_never_frees_own_site(self):
        for host in ("jean.free.fr", "subscribe.free.fr.pirate.invalid", "x.subscribe.free.fr", "monfree.fr"):
            with self.subTest(host=host):
                self.assertFalse(same_site(host, "subscribe.free.fr"))
                self.assertFalse(same_site("adsl.free.fr", host))

    def test_the_customers_of_the_platforms_added_since_are_two_sites(self):
        """A region, a storage account or a tenant before the platform's
        own name: every customer under it is a site of its own."""
        for host, other in (
            ("factures.westeurope.cloudapp.azure.com", "pirate.westeurope.cloudapp.azure.com"),
            ("factures.blob.core.windows.net", "pirate.blob.core.windows.net"),
            ("acme.sharepoint.com", "pirate.sharepoint.com"),
            ("acme.onmicrosoft.com", "pirate.onmicrosoft.com"),
            ("acme.zendesk.com", "pirate.zendesk.com"),
            ("acme.atlassian.net", "pirate.atlassian.net"),
            ("acme.service-now.com", "pirate.service-now.com"),
            ("plombier.e-monsite.com", "pirate.e-monsite.com"),
            ("plombier.webnode.fr", "pirate.webnode.fr"),
            ("1a2b-3c4d.ngrok-free.app", "5e6f-7a8b.ngrok-free.app"),
            ("pub-1a2b.r2.dev", "pub-3c4d.r2.dev"),
        ):
            with self.subTest(host=host, other=other):
                self.assertFalse(same_site(host, other))
                self.assertTrue(same_site(host, host.upper()))
        # The platform's own hosts beside them are not under the suffix.
        self.assertTrue(same_site("portal.azure.com", "login.azure.com"))

    def test_a_name_only_ending_like_a_shared_suffix_is_an_ordinary_site(self):
        """Matched label by label: « monfree.fr » is not under « free.fr »."""
        self.assertTrue(same_site("www.monfree.fr", "espace.monfree.fr"))
        self.assertTrue(same_site("www.notgithub.io", "espace.notgithub.io"))

    def test_two_sites(self):
        for host, other in (
            ("evil.example", "free.fr"),
            ("x.co.uk", "y.co.uk"),
            ("impots.gouv.fr", "pirate.gouv.fr"),
            ("free.fr.evil.example", "free.fr"),
            ("notfree.fr", "free.fr"),
            ("mairie.asso.fr", "pirate.asso.fr"),
        ):
            with self.subTest(host=host, other=other):
                self.assertFalse(same_site(host, other))

    def test_the_registrable_domain(self):
        self.assertEqual(registrable_domain("particulier.edf.fr"), "edf.fr")
        self.assertEqual(registrable_domain("jean.free.fr"), "jean.free.fr")
        self.assertEqual(registrable_domain("Mobile.Free.fr."), "free.fr")
        self.assertEqual(registrable_domain("Fournisseur.GitHub.io."), "fournisseur.github.io")
        self.assertEqual(registrable_domain("a.b.exemple.co.uk"), "exemple.co.uk")
        self.assertEqual(registrable_domain("www.impots.gouv.fr"), "impots.gouv.fr")
        # A second level of that list counts only under a two-letter
        # country: « com.org » is a site of its own.
        self.assertEqual(registrable_domain("www.com.org"), "com.org")
        self.assertEqual(registrable_domain("localhost"), "localhost")

    def test_an_address_is_its_own_site(self):
        """Read as labels, 10.0.0.1 and 127.0.0.1 would both end in « 0.1 »."""
        self.assertTrue(same_site("127.0.0.1", "127.0.0.1"))
        self.assertFalse(same_site("10.0.0.1", "127.0.0.1"))
        self.assertFalse(same_site("::1", "0::2"))

    def test_nothing_is_no_site(self):
        self.assertFalse(same_site("", ""))
        self.assertFalse(same_site("", "free.fr"))


class PortalHostTests(SimpleTestCase):
    """The host a portal's password is bound to (invoices.models): https,
    and no port but 443 - another port is another server."""

    def test_an_https_address_names_its_host(self):
        self.assertEqual(portal_host("https://Box.Exemple.Invalid./login"), SITE)
        self.assertEqual(portal_host(f"https://{SITE}:443/login"), SITE)

    def test_another_port_is_no_portal(self):
        for url in (f"https://{SITE}:8443/login", f"https://{SITE}:80/login", f"https://{SITE}:99999/login"):
            with self.subTest(url=url):
                self.assertEqual(portal_host(url), "")

    def test_an_address_that_is_not_https_is_no_portal(self):
        for url in (f"http://{SITE}/login", f"ftp://{SITE}/", f"{SITE}/login", "https:///login", ""):
            with self.subTest(url=url):
                self.assertEqual(portal_host(url), "")


# ----------------------------------------------------------- credentials


class NoStore:
    """The test espace's private folder is the whole run's: no test may
    leave a stored password there (accounts/tests/test_credentials.py)."""

    def setUp(self):
        super().setUp()
        self.forget()
        self.addCleanup(self.forget)

    @staticmethod
    def forget():
        folder = paths.private_dir()
        for name in vault.FILE_NAMES:
            (folder / name).unlink(missing_ok=True)
        for leftover in folder.glob(vault.TEMPORARY_PREFIX + "*"):
            leftover.unlink()


class StoredCredentialsTests(NoStore, NoNetworkTestCase):
    def store(self, bindings=None):
        vault.save({"BOX_LOGIN": LOGIN, "BOX_PASSWORD": SECRET}, bindings=bindings)

    def refused(self, source, env_file=None, environ=None):
        with self.assertRaises(WebsiteError) as raised:
            credentials(source, env_file=env_file, environ=environ or {})
        message = str(raised.exception)
        for value in (SECRET, LOGIN):
            self.assertNotIn(value, message)
        return message

    def test_values_typed_for_this_site_are_given(self):
        self.store({"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        self.assertEqual(credentials(recipe(), env_file=None, environ={}), (LOGIN, SECRET))

    def test_values_typed_for_another_site_are_refused_naming_the_site_not_the_value(self):
        """A source pointed at another page since the password was typed -
        by its form, an import, the admin - gets nothing."""
        self.store({"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        message = self.refused(recipe(login_url="https://pirate.invalid/login"))
        self.assertIn("saisi pour un autre site", message)
        self.assertIn(f"({SITE})", message)
        self.assertIn("page Identifiants", message)

    def test_another_host_of_the_same_site_is_still_another_site_for_a_stored_value(self):
        """The binding is the host the password was typed for, exactly."""
        self.store({"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        self.refused(recipe(login_url=f"https://espace.{SITE}/login"))

    def test_a_stored_value_with_no_site_recorded_is_refused(self):
        self.store()
        message = self.refused(recipe())
        self.assertIn("(aucun)", message)

    def test_one_stored_value_out_of_place_is_enough(self):
        self.store({"BOX_LOGIN": SITE, "BOX_PASSWORD": "autre.invalid"})
        self.assertIn("(autre.invalid)", self.refused(recipe()))

    def test_a_value_refused_is_not_taken_from_the_env_file_instead(self):
        """Typed for another site, it is to be typed again - not replaced
        in silence by whatever the .env says."""
        self.store({"BOX_LOGIN": "autre.invalid", "BOX_PASSWORD": "autre.invalid"})
        vault.save({}, env_bindings={"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        self.refused(recipe(), environ={"BOX_LOGIN": "env", "BOX_PASSWORD": "env-secret"})

    def test_the_env_file_and_the_environment_answer_for_the_site_the_owner_confirmed(self):
        """The server's own file, the owner's setup before « Identifiants »:
        given to the site the owner confirmed on that page for each name."""
        vault.save({}, env_bindings={"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("BOX_LOGIN=depuis-le-fichier\nBOX_PASSWORD=secret-du-fichier\n")
            self.assertEqual(
                credentials(recipe(), env_file=path, environ={}), ("depuis-le-fichier", "secret-du-fichier")
            )
        self.assertEqual(
            credentials(recipe(), env_file=None, environ={"BOX_LOGIN": "env", "BOX_PASSWORD": "env-secret"}),
            ("env", "env-secret"),
        )

    def test_values_of_the_env_with_no_site_confirmed_are_refused(self):
        """A .env value carries no site of its own: a source pointed at any
        page would have it typed there."""
        environ = {"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"}
        with self.assertRaises(WebsiteError) as raised:
            credentials(recipe(), env_file=None, environ=environ)
        message = str(raised.exception)
        self.assertEqual(
            message,
            f"Box Exemple : confirmez sur la page Identifiants que {SITE} est bien le site de BOX_LOGIN et "
            "BOX_PASSWORD (valeurs du fichier .env), ou saisissez-y le mot de passe.",
        )
        for value in environ.values():
            self.assertNotIn(value, message)

    def test_values_of_the_env_confirmed_for_another_site_are_refused(self):
        vault.save({}, env_bindings={"BOX_LOGIN": "autre.invalid", "BOX_PASSWORD": "autre.invalid"})
        with self.assertRaises(WebsiteError) as raised:
            credentials(recipe(), env_file=None, environ={"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"})
        self.assertIn(f"que {SITE} est bien le site de BOX_LOGIN et BOX_PASSWORD", str(raised.exception))
        self.assertNotIn("env-secret", str(raised.exception))

    def test_only_the_name_still_to_confirm_is_named(self):
        vault.save({}, env_bindings={"BOX_LOGIN": SITE})
        with self.assertRaises(WebsiteError) as raised:
            credentials(recipe(), env_file=None, environ={"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"})
        self.assertEqual(
            str(raised.exception),
            f"Box Exemple : confirmez sur la page Identifiants que {SITE} est bien le site de BOX_PASSWORD "
            "(valeur du fichier .env), ou saisissez-y le mot de passe.",
        )

    def test_another_host_of_the_same_site_is_another_site_for_a_value_of_the_env(self):
        vault.save({}, env_bindings={"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        with self.assertRaises(WebsiteError):
            credentials(
                recipe(login_url=f"https://espace.{SITE}/login"),
                env_file=None,
                environ={"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"},
            )

    def test_a_stored_login_bound_here_goes_with_a_password_from_the_env(self):
        vault.save({"BOX_LOGIN": LOGIN}, bindings={"BOX_LOGIN": SITE}, env_bindings={"BOX_PASSWORD": SITE})
        self.assertEqual(
            credentials(recipe(), env_file=None, environ={"BOX_PASSWORD": "env-secret"}), (LOGIN, "env-secret")
        )

    def test_a_stored_login_bound_here_needs_the_env_password_confirmed_too(self):
        vault.save({"BOX_LOGIN": LOGIN}, bindings={"BOX_LOGIN": SITE})
        message = self.refused(recipe(), environ={"BOX_PASSWORD": "env-secret"})
        self.assertIn("est bien le site de BOX_PASSWORD (valeur du fichier .env)", message)
        self.assertNotIn("env-secret", message)

    def test_a_value_typed_as_a_password_is_never_typed_as_a_login(self):
        """A source naming another account's password as its identifier
        would type it in clear, into a text field, on its page."""
        vault.save(
            {"BOX_LOGIN": SECRET, "BOX_PASSWORD": "autre-secret"},
            bindings={"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE},
            secret_names=("BOX_LOGIN", "BOX_PASSWORD"),
        )
        message = self.refused(recipe())
        self.assertIn("BOX_LOGIN", message)
        self.assertIn("mot de passe", message)
        self.assertNotIn("autre-secret", message)

    def test_a_store_being_rewritten_sends_nothing(self):
        """Read while Windows holds the file: not the .env's values in its
        place - what is stored may say otherwise."""
        environ = {"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"}
        with mock.patch.object(vault, "load", return_value=vault.VaultState(problem=vault.BUSY)):
            with self.assertRaises(WebsiteError) as raised:
                credentials(recipe(), env_file=None, environ=environ)
        self.assertEqual(str(raised.exception), vault.BUSY_MESSAGE)

    def test_a_store_this_server_cannot_open_sends_nothing(self):
        """Its bindings and confirmations are unknown: nothing goes, the
        .env's values included, until the page starts again."""
        (paths.private_dir() / vault.FILE_NAME).write_bytes(b"pas un magasin")
        environ = {"BOX_LOGIN": "env-login", "BOX_PASSWORD": "env-secret"}
        self.assertEqual(vault.load().problem, vault.UNREADABLE)
        with self.assertRaises(WebsiteError) as raised:
            credentials(recipe(), env_file=None, environ=environ)
        self.assertEqual(
            str(raised.exception),
            "Les identifiants enregistrés ne peuvent pas être lus sur ce serveur : aucun mot de passe n'est envoyé "
            "aux espaces clients avant « Tout ressaisir » sur la page Identifiants.",
        )

    def test_no_store_at_all_is_no_refusal_of_its_own(self):
        """Nothing stored yet: the source's own words, what is missing."""
        with self.assertRaises(WebsiteError) as raised:
            credentials(recipe(), env_file=None, environ={})
        self.assertIn("manquent", str(raised.exception))

    def test_what_is_missing_is_asked_for_in_the_singular_or_the_plural(self):
        for environ, said in (
            (
                {},
                (
                    "Box Exemple : l'identifiant et le mot de passe manquent - renseignez-les sur la page "
                    "Identifiants (ou BOX_LOGIN et BOX_PASSWORD dans le fichier .env)."
                ),
            ),
            (
                {"BOX_LOGIN": LOGIN},
                (
                    "Box Exemple : le mot de passe manque - renseignez-le sur la page Identifiants "
                    "(ou BOX_PASSWORD dans le fichier .env)."
                ),
            ),
            (
                {"BOX_PASSWORD": SECRET},
                (
                    "Box Exemple : l'identifiant manque - renseignez-le sur la page Identifiants "
                    "(ou BOX_LOGIN dans le fichier .env)."
                ),
            ),
        ):
            with self.subTest(environ=sorted(environ)):
                self.assertEqual(self.refused(recipe(), environ=environ), said)

    def test_a_login_page_on_another_port_says_so(self):
        """https all the same: « n'est pas une adresse https » read wrong."""
        environ = {"BOX_LOGIN": LOGIN, "BOX_PASSWORD": SECRET}
        message = self.refused(recipe(login_url=f"https://{SITE}:8443/login"), environ=environ)
        self.assertEqual(
            message,
            "La page de connexion de Box Exemple est sur un autre port (8443) que celui de https (443) : aucun "
            "mot de passe n'y est envoyé.",
        )

    def test_a_login_page_that_is_not_https_gets_no_password(self):
        """Even the .env's: over http it travels in clear."""
        self.store({"BOX_LOGIN": "box.exemple.invalid", "BOX_PASSWORD": "box.exemple.invalid"})
        environ = {"BOX_LOGIN": LOGIN, "BOX_PASSWORD": SECRET}
        for url in ("http://box.exemple.invalid/login", "ftp://box.exemple.invalid/", "box.exemple.invalid/login", ""):
            with self.subTest(url=url):
                message = self.refused(recipe(login_url=url), environ=environ)
                self.assertIn("n'est pas une adresse https : aucun mot de passe n'y est envoyé", message)

    def test_one_name_for_the_login_and_the_password_is_refused(self):
        """The password would be typed into the identifier's field - in
        clear on the page, and in its HTML."""
        message = self.refused(
            recipe(username_env="BOX_SECRET", password_env="BOX_SECRET"), environ={"BOX_SECRET": SECRET}
        )
        self.assertIn("même nom", message)

    def test_the_older_refusals_still_come_first(self):
        message = self.refused(recipe(login_url="http://box.exemple.invalid/", username_env="METRO_EMAIL"))
        self.assertIn("« METRO_EMAIL » est une variable de l'application", message)


# ------------------------------------------------------------- signing in


class SignInGuardTests(QuickCase):
    """Checked on the page as it is, just before each value is typed."""

    def sign_in(self, browser, **settings):
        with tempfile.TemporaryDirectory() as folder:
            visit = website._Visit(recipe(**settings), folder, self.logged.append, None, lambda *a: browser, True)
            visit.sign_in(LOGIN, SECRET)

    def refused(self, browser, **settings):
        with self.assertRaises(WebsiteError) as raised:
            self.sign_in(browser, **settings)
        message = str(raised.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(SECRET, "\n".join(self.logged))
        return message

    def typed(self, field):
        return "".join(str(key) for key in field.typed)

    def test_a_login_page_moving_within_its_site_is_signed_into(self):
        browser = FakeBrowser(lands_on=f"https://connexion.{SITE}/sso?retour=1")
        self.sign_in(browser)
        self.assertEqual((self.typed(browser.user), self.typed(browser.password)), (LOGIN, SECRET))
        self.assertIn("Box Exemple : connecté.", self.logged)

    def test_a_login_page_ending_on_another_site_gets_nothing_typed(self):
        browser = FakeBrowser(lands_on="https://box-exemple.pirate.invalid/login")
        message = self.refused(browser)
        self.assertIn("box-exemple.pirate.invalid", message)
        self.assertIn("rien n'y a été saisi", message)
        self.assertEqual((browser.user.typed, browser.password.typed), ([], []))

    def test_a_login_page_on_its_own_host_without_https_gets_nothing_typed(self):
        browser = FakeBrowser(lands_on=f"http://{SITE}/login")
        message = self.refused(browser)
        self.assertIn(f"l'identifiant ({SITE}) n'est pas en https : rien n'y a été saisi", message)
        self.assertEqual((browser.user.typed, browser.password.typed), ([], []))

    def test_a_login_page_on_another_port_of_its_host_says_so_and_gets_nothing_typed(self):
        """https all the same: « n'est pas en https » read wrong."""
        for address, port in ((f"https://{SITE}:8443/login", " (8443)"), (f"https://{SITE}:99999/login", "")):
            with self.subTest(address=address):
                browser = FakeBrowser(lands_on=address)
                message = self.refused(browser)
                self.assertEqual(
                    message,
                    f"Box Exemple : la page où saisir l'identifiant ({SITE}) est sur un autre port{port} que celui "
                    "de https (443) : rien n'y a été saisi.",
                )
                self.assertEqual((browser.user.typed, browser.password.typed), ([], []))

    def test_the_password_page_of_a_two_step_login_on_another_site_gets_no_password(self):
        browser = FakeBrowser()
        password, button = Field("password"), Field("submit", tag="button")
        browser.form = [browser.user, None, browser.button]
        browser.next_form = [None, password, button]
        browser.sends_to = "https://identification.pirate.invalid/mot-de-passe"
        message = self.refused(browser)
        self.assertIn("identification.pirate.invalid", message)
        self.assertEqual((self.typed(browser.user), password.typed), (LOGIN, []))

    def test_a_page_leaving_its_site_while_the_login_is_typed_gets_no_password(self):
        """Checked again before the password: one key typed may be enough
        for a page's script to move it."""
        browser = FakeBrowser()

        def leave():
            browser.current_url = "https://pirate.invalid/login"

        browser.user.on_typed = leave
        self.refused(browser)
        self.assertEqual(browser.password.typed, [])

    def test_a_password_selector_naming_another_field_types_nothing(self):
        """The login form script refuses a selector naming anything but an
        <input type=password> (the fake answers as it does)."""
        browser = FakeBrowser(form=website.NOT_A_PASSWORD_FIELD)
        message = self.refused(browser, password_selector="#identifiant")
        self.assertIn("le sélecteur du mot de passe ne désigne pas un champ mot de passe", message)

    def test_the_script_says_so_for_the_selector_it_is_given(self):
        self.assertIn(f"'{website.NOT_A_PASSWORD_FIELD}'", website.FIND_LOGIN_JS)
        self.assertIn(f"'{website.LOGIN_IS_A_PASSWORD_FIELD}'", website.FIND_LOGIN_JS)

    def test_a_password_field_that_is_not_one_any_more_gets_no_password(self):
        """Checked again on the element itself, just before typing: a page
        may turn the field into a text box (an eye « afficher »)."""
        browser = FakeBrowser()
        browser.password.kind = "text"
        message = self.refused(browser)
        self.assertIn("ne désigne pas un champ mot de passe", message)
        self.assertEqual(browser.password.typed, [])

    def test_a_password_field_must_be_an_input(self):
        browser = FakeBrowser()
        browser.password.tag_name = "div"
        self.refused(browser)
        self.assertEqual(browser.password.typed, [])

    def test_the_login_is_never_typed_into_a_password_field(self):
        browser = FakeBrowser(form=website.LOGIN_IS_A_PASSWORD_FIELD)
        self.assertIn("champ mot de passe", self.refused(browser, username_selector="#mdp"))
        browser = FakeBrowser()
        browser.user.kind = "password"
        self.refused(browser)
        self.assertEqual((browser.user.typed, browser.password.typed), ([], []))


# --------------------------------------------------------- the kept page


class KeptPageTests(NoStore, NoNetworkTestCase):
    """A failure keeps a screenshot and the page's HTML under _debug/ - how
    a site that changed is fixed. Neither may hold what was typed."""

    PAGE = (
        "<html><body><form>"
        f'<input name="u" value="{LOGIN}">'
        '<input type="password" name="p" value="Pa&quot;ss&lt;w&gt;&amp;ord-é">'
        '<input name="copie" value=\'Pa"ss<w>&ord-é\'>'
        '<input name="echo" value=\'Pa"ss&lt;w&gt;&amp;ord-é\'>'
        "</form>"
        '<script>window.__ETAT__ = {"motDePasse": "Pa\\"ss<w>&ord-\\u00e9"};</script>'
        '<a href="/reessayer?pwd=Pa%22ss%3Cw%3E%26ord-%C3%A9">Réessayer</a>'
        "</body></html>"
    )

    def setUp(self):
        super().setUp()
        for patcher in quick():
            patcher.start()
            self.addCleanup(patcher.stop)
        environ = mock.patch.dict(os.environ, {"BOX_LOGIN": LOGIN, "BOX_PASSWORD": SECRET})
        environ.start()
        self.addCleanup(environ.stop)
        vault.save({}, env_bindings={"BOX_LOGIN": SITE, "BOX_PASSWORD": SITE})
        # A source's folder inside a downloads folder of the test's own: a
        # visit prunes the folders beside its own.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.folder = os.path.join(root, "type-1")
        self.logged = []

    def fail_signing_in(self, browser, password=SECRET):
        with mock.patch.dict(os.environ, {"BOX_PASSWORD": password}):
            with self.assertRaises(WebsiteError) as raised:
                fetch_website_invoices(
                    recipe(),
                    self.folder,
                    date(2026, 5, 1),
                    date(2026, 5, 31),
                    log=self.logged.append,
                    driver_factory=lambda *args: browser,
                    env_file=None,
                )
        return str(raised.exception)

    def wrong_password_page(self, **kwargs):
        settings = {
            "signs_in": False,
            "sends_to": f"{LOGIN_URL}?pwd=Pa%22ss%3Cw%3E%26ord-%C3%A9&u={LOGIN}#essai",
            "source": self.PAGE,
            "error_text": f"Mot de passe {SECRET} incorrect pour {LOGIN}",
        }
        settings.update(kwargs)
        return FakeBrowser(**settings)

    def written(self):
        found = {}
        for folder, _dirs, names in os.walk(self.folder):
            for name in names:
                with open(os.path.join(folder, name), "rb") as handle:
                    found[name] = handle.read().decode("utf-8", errors="replace")
        return found

    def test_nothing_written_said_or_raised_holds_what_was_typed(self):
        message = self.fail_signing_in(self.wrong_password_page())
        self.assertIn("la connexion n'a pas abouti", message)
        files = self.written()
        self.assertEqual(sorted(name.rsplit(".", 1)[1] for name in files), ["html", "png"], files)
        said = "\n".join(self.logged)
        for form in (*SECRET_FORMS, LOGIN):
            with self.subTest(form=form):
                self.assertNotIn(form, message)
                self.assertNotIn(form, said)
                for name, content in files.items():
                    self.assertNotIn(form, content, name)
        # What is not a secret stays, for whoever fixes the site's settings.
        (page,) = (content for name, content in files.items() if name.endswith(".html"))
        self.assertIn("Réessayer", page)
        self.assertIn(website.MASK, page)

    def kinds(self):
        return sorted(name.rsplit(".", 1)[1] for name in self.written())

    def test_the_fields_are_emptied_and_the_screen_scrubbed_before_the_screenshot_and_the_html(self):
        browser = self.wrong_password_page()
        self.fail_signing_in(browser)
        self.assertEqual(browser.events, ["blank", "scrub", "text", "screenshot", "source"])
        values, mask, loose = browser.scrubbed_with
        self.assertEqual((values, mask), ([LOGIN, SECRET], website.MASK))
        # The login's spellings, matched whatever their case; never the
        # password's.
        self.assertEqual(sorted(loose), sorted({LOGIN, LOGIN.upper()}))

    def test_the_login_echoed_in_another_case_is_kept_nowhere(self):
        """A site prints the login back as it stores it - lowercased, or
        in capitals in a header: masked whatever its case, on screen, in the
        HTML, in the log and the error."""
        shouted, titled = LOGIN.upper(), LOGIN.title()
        browser = self.wrong_password_page(
            source=f"<html><body><h1>{shouted}</h1><p>Aucun compte pour {titled}</p>"
            '<a href="/aide?u=GERANT%40EXEMPLE.INVALID">Aide</a></body></html>',
            error_text=f"Aucun compte pour {shouted}",
            screen=[f"Bienvenue {shouted}", f"Aucun compte pour {titled}"],
        )
        message = self.fail_signing_in(browser)
        self.assertEqual(self.kinds(), ["html", "png"])
        self.assertEqual(browser.screen, [f"Bienvenue {website.MASK}", f"Aucun compte pour {website.MASK}"])
        (page,) = (content for name, content in self.written().items() if name.endswith(".html"))
        for text in (message, "\n".join(self.logged), page):
            self.assertNotIn(LOGIN, text.casefold())
            self.assertNotIn("gerant%40exemple.invalid", text.casefold())
        self.assertIn("Aide", page)

    def test_a_login_split_across_elements_is_neither_photographed_nor_written(self):
        """« <b>Gerant</b>@exemple.invalid »: in no text node whole, so the
        screen scrub finds nothing - but the page shows it whole."""
        browser = self.wrong_password_page(screen=["Aucun compte pour ", "Gerant", "@exemple.invalid"])
        self.fail_signing_in(browser)
        self.assertEqual(self.written(), {})
        self.assertNotIn("screenshot", browser.events)
        self.assertNotIn("source", browser.events)
        self.assertTrue(
            any("page non enregistrée : son texte affiché contenait un identifiant" in line for line in self.logged),
            self.logged,
        )

    def test_a_page_whose_text_cannot_be_read_is_not_kept(self):
        browser = self.wrong_password_page(text_fails=True)
        self.fail_signing_in(browser)
        self.assertEqual(self.written(), {})
        self.assertNotIn("screenshot", browser.events)
        self.assertTrue(
            any("page non enregistrée : son texte n'a pas pu être vérifié" in line for line in self.logged), self.logged
        )

    def test_a_login_split_by_tags_in_the_html_is_not_written(self):
        """Hidden from the screen - a menu drawn only on a click - but in
        the file: neither is kept, nor its path said."""
        browser = self.wrong_password_page(
            source="<html><body><nav hidden><b>Gerant</b>@exemple.<i>invalid</i></nav></body></html>", error_text=""
        )
        self.fail_signing_in(browser)
        self.assertIn("screenshot", browser.events)
        self.assertEqual(self.written(), {})
        self.assertTrue(any("page non enregistrée : elle contenait un identifiant" in line for line in self.logged))
        self.assertFalse(any(".png" in line for line in self.logged), self.logged)

    def test_what_the_page_shows_is_scrubbed_of_what_was_typed_before_its_screenshot(self):
        """A text echoing the login (« Aucun compte pour … ») shows in a
        screenshot as plainly as in the HTML."""
        browser = self.wrong_password_page(screen=[f"Aucun compte pour {LOGIN}", f"Mot de passe « {SECRET} » refusé"])
        self.fail_signing_in(browser)
        self.assertEqual(
            browser.screen, [f"Aucun compte pour {website.MASK}", f"Mot de passe « {website.MASK} » refusé"]
        )
        self.assertEqual(self.kinds(), ["html", "png"])

    def test_a_page_still_showing_a_value_typed_is_not_photographed(self):
        """What the scrub could not rewrite - an attribute (a title, an
        alt), a text holding the mask's own letters: no screenshot, said."""
        browser = self.wrong_password_page(screen_attributes=[f"Connecté en tant que {LOGIN}"])
        self.fail_signing_in(browser)
        self.assertNotIn("screenshot", browser.events)
        self.assertEqual(self.kinds(), ["html"])
        self.assertTrue(
            any("capture non enregistrée : elle contenait un identifiant" in line for line in self.logged), self.logged
        )
        self.assertFalse(any(".png" in line for line in self.logged), self.logged)

    def test_a_page_whose_screen_cannot_be_checked_is_not_photographed(self):
        browser = self.wrong_password_page(scrub_fails=True)
        self.fail_signing_in(browser)
        self.assertNotIn("screenshot", browser.events)
        self.assertEqual(self.kinds(), ["html"])
        self.assertTrue(any("capture non enregistrée" in line for line in self.logged), self.logged)

    def test_an_address_said_in_the_log_loses_its_query_and_fragment(self):
        self.fail_signing_in(self.wrong_password_page())
        said = "\n".join(self.logged)
        self.assertIn(f"adresse : {LOGIN_URL})", said)
        self.assertNotIn("pwd=", said)
        self.assertNotIn("#essai", said)

    def test_a_password_cut_by_the_quoted_message_is_masked_first(self):
        """The page's error is quoted cut to 200 characters: cut first, the
        start of the password would no longer read as the password."""
        browser = self.wrong_password_page(error_text="x" * 195 + SECRET)
        message = self.fail_signing_in(browser)
        self.assertNotIn(SECRET[:5], message)

    def test_the_links_named_in_an_error_do_not_name_the_account(self):
        """Signed in, the page's links are named when the one to follow is
        not there - and a portal's menu often names the account."""
        link = {"index": 0, "text": f"Mon compte ({LOGIN})", "href": "", "row": "", "download": False}
        browser = FakeBrowser(links=[{**link, "label": "", "holds": []}])
        with self.assertRaises(WebsiteError) as raised:
            fetch_website_invoices(
                recipe(navigation=["Mes factures"]),
                self.folder,
                date(2026, 5, 1),
                date(2026, 5, 31),
                log=self.logged.append,
                driver_factory=lambda *args: browser,
                env_file=None,
            )
        self.assertIn("Mon compte", str(raised.exception))
        self.assertNotIn(LOGIN, str(raised.exception))
        self.assertNotIn(LOGIN, "\n".join(self.logged))

    def test_a_link_followed_is_said_without_the_accounts_name(self):
        """« lien « … » » in the job's log: the portal's link to the
        invoices may carry the account's address."""
        link = {"index": 0, "text": f"Factures de {LOGIN}", "href": "", "row": "", "download": False}
        browser = FakeBrowser(links=[{**link, "label": "", "holds": []}])
        with self.assertRaises(WebsiteError):
            fetch_website_invoices(
                recipe(),
                self.folder,
                date(2026, 5, 1),
                date(2026, 5, 31),
                log=self.logged.append,
                driver_factory=lambda *args: browser,
                env_file=None,
            )
        self.assertIn(f"Box Exemple : lien « Factures de {website.MASK} ».", self.logged)
        self.assertNotIn(LOGIN, "\n".join(self.logged))

    def test_a_page_the_masking_cannot_clean_is_not_written_nor_its_screenshot(self):
        """A value that the mask itself holds (« asqu » in « [masqué] ») is
        still in the page once masked: the page is not kept at all - nor
        the screenshot taken a moment before, whose path is not said."""
        browser = self.wrong_password_page(source='<input type="password" value="asqu">', error_text="")
        self.fail_signing_in(browser, password="asqu")
        self.assertIn("screenshot", browser.events)
        self.assertEqual(self.written(), {})
        self.assertTrue(any("page non enregistrée : elle contenait un identifiant" in line for line in self.logged))
        self.assertFalse(any(".png" in line for line in self.logged), self.logged)

    def test_a_page_whose_fields_cannot_be_emptied_is_not_kept(self):
        browser = self.wrong_password_page()
        execute = browser.execute_script

        def refusing(script, *args):
            if script is website.BLANK_FIELDS_JS:
                raise RuntimeError("javascript error")
            return execute(script, *args)

        browser.execute_script = refusing
        self.fail_signing_in(browser)
        self.assertEqual(self.written(), {})
        self.assertTrue(any("page non enregistrée" in line for line in self.logged), self.logged)
        self.assertNotIn("screenshot", browser.events)
        self.assertNotIn("source", browser.events)


class SessionFetchTests(SimpleTestCase):
    """A link naming its file is fetched with the browser's cookies
    (`_Visit._fetch`, without a click to wait on): only over https, only on
    the portal's own site, and each cookie only where the browser itself
    would send it - a session cookie never goes over http, nor to another
    host. Nothing reaches the network: requests' transport is replaced."""

    COOKIES = (
        {"name": "session", "value": "jeton-de-session", "domain": SITE, "path": "/", "secure": True, "httpOnly": True},
        {"name": "espace", "value": "jeton-espace", "domain": SITE, "path": "/espace", "secure": True},
        {"name": "suivi", "value": "jeton-du-site", "domain": ".exemple.invalid", "path": "/", "secure": False},
    )

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.browser = FakeBrowser(cookies=self.COOKIES)
        self.browser.current_url = f"https://{SITE}/factures"
        self.logged = []
        self.sent = []
        self.answers = {}

    def visit(self, **settings):
        return website._Visit(recipe(**settings), self.folder, self.logged.append, None, lambda *a: self.browser, True)

    def send(self, adapter, request, **kwargs):
        """The transport: what was asked is kept, the answer given."""
        self.sent.append(request)
        status, headers, content = self.answers.get(request.url, (200, {}, b"%PDF-1.4 facture"))
        response = requests.Response()
        response.status_code = status
        response.headers = requests.structures.CaseInsensitiveDict(headers)
        response._content = content
        response.url = request.url
        response.request = request
        return response

    def fetch(self, url, visit=None):
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=self.send):
            return (visit or self.visit())._fetch(url)

    def cookies_sent(self, index=0):
        header = self.sent[index].headers.get("Cookie", "")
        return sorted(part.split("=", 1)[0] for part in header.split("; ") if part)

    def test_an_invoice_on_the_portals_own_host_is_fetched_with_its_session(self):
        name = self.fetch(f"https://{SITE}/factures/2026-05.pdf")
        self.assertTrue(name)
        with open(os.path.join(self.folder, name), "rb") as handle:
            self.assertEqual(handle.read(), b"%PDF-1.4 facture")
        # Not « espace »: its path is /espace.
        self.assertEqual(self.cookies_sent(), ["session", "suivi"])

    def test_a_cookie_keeps_its_path(self):
        self.fetch(f"https://{SITE}/espace/factures/2026-05.pdf")
        self.assertEqual(self.cookies_sent(), ["espace", "session", "suivi"])

    def test_a_cookie_keeps_its_domain(self):
        """Another host of the portal's site gets the cookies set for the
        whole site, never the portal host's own."""
        self.assertTrue(self.fetch("https://documents.exemple.invalid/2026-05.pdf"))
        self.assertEqual(self.cookies_sent(), ["suivi"])

    def test_a_link_to_another_site_is_never_fetched_with_the_session(self):
        self.assertIsNone(self.fetch("https://pirate.invalid/2026-05.pdf"))
        self.assertEqual(self.sent, [])
        self.assertTrue(any("pirate.invalid" in line for line in self.logged), self.logged)

    def test_links_off_the_site_are_said_once_a_run_not_once_an_invoice(self):
        visit = self.visit()
        for month in ("2026-03", "2026-04", "2026-05"):
            self.assertIsNone(self.fetch(f"https://pirate.invalid/{month}.pdf", visit=visit))
        self.assertIsNone(self.fetch(f"http://{SITE}/2026-05.pdf", visit=visit))
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.logged,
            [
                (
                    "Box Exemple : lien hors du site du portail ou sans https (pirate.invalid) : pas téléchargé avec "
                    "la session du navigateur (signalé une fois par récupération)."
                )
            ],
        )
        # A run of its own says it again.
        self.assertIsNone(self.fetch("https://pirate.invalid/2026-06.pdf"))
        self.assertEqual(len(self.logged), 2)

    def test_a_freebox_invoice_is_fetched_from_frees_invoice_host(self):
        """Signed in at subscribe.free.fr, its invoices are links to
        adsl.free.fr: Free's own hosts are one site."""
        self.browser.cookies = [
            {"name": "session", "value": "jeton", "domain": ".free.fr", "path": "/", "secure": True},
        ]
        visit = self.visit(login_url="https://subscribe.free.fr/login/")
        url = "https://adsl.free.fr/facture_pdf.pl?id=1&no_facture=2026050001"
        self.assertTrue(self.fetch(url, visit=visit))
        self.assertEqual([request.url for request in self.sent], [url])
        self.assertEqual(self.cookies_sent(), ["session"])

    def test_a_subscribers_page_under_free_fr_is_never_fetched_with_frees_session(self):
        visit = self.visit(login_url="https://subscribe.free.fr/login/")
        self.assertIsNone(self.fetch("https://jean.free.fr/facture.pdf", visit=visit))
        self.assertEqual(self.sent, [])

    def test_a_link_over_http_is_never_fetched(self):
        for url in (f"http://{SITE}/factures/2026-05.pdf", f"https://{SITE}:8443/factures/2026-05.pdf"):
            with self.subTest(url=url):
                self.assertIsNone(self.fetch(url))
        self.assertEqual(self.sent, [])

    def test_a_redirect_leaving_the_site_or_https_is_not_followed(self):
        start = f"https://{SITE}/factures/2026-05.pdf"
        for target in ("https://pirate.invalid/2026-05.pdf", f"http://{SITE}/2026-05.pdf"):
            with self.subTest(target=target):
                self.sent = []
                self.answers = {start: (302, {"Location": target}, b"")}
                self.assertIsNone(self.fetch(start))
                self.assertEqual([request.url for request in self.sent], [start])

    def test_a_redirect_within_the_site_is_followed_with_the_session(self):
        start = f"https://{SITE}/factures/2026-05.pdf"
        self.answers = {start: (302, {"Location": "/telechargement/2026-05.pdf"}, b"")}
        self.assertTrue(self.fetch(start))
        self.assertEqual([request.url for request in self.sent], [start, f"https://{SITE}/telechargement/2026-05.pdf"])
        self.assertIn("session", self.cookies_sent(1))

    def test_redirects_end(self):
        start = f"https://{SITE}/a.pdf"
        self.answers = {start: (302, {"Location": start}, b"")}
        self.assertIsNone(self.fetch(start))
        self.assertLessEqual(len(self.sent), 10)

    def test_a_secure_cookie_never_goes_over_http(self):
        """The test settings' local portal is served over http
        (PORTAL_PLAIN_HTTP_HOSTS): a cookie marked secure stays home."""
        self.browser.cookies = [
            {"name": "session", "value": "jeton", "domain": "127.0.0.1", "path": "/", "secure": True},
            {"name": "langue", "value": "fr", "domain": "127.0.0.1", "path": "/", "secure": False},
        ]
        visit = self.visit(login_url="http://127.0.0.1:8123/login")
        self.assertTrue(self.fetch("http://127.0.0.1:8123/pdf/2026-05.pdf", visit=visit))
        self.assertEqual(self.cookies_sent(), ["langue"])

    def test_a_tab_showing_another_sites_document_is_not_fetched(self):
        browser = self.browser
        tabs = {"onglet": "https://pirate.invalid/facture.pdf", "main": f"https://{SITE}/factures"}
        browser.switch_to = types.SimpleNamespace(window=lambda handle: setattr(browser, "current_url", tabs[handle]))
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=self.send):
            self.assertIsNone(self.visit()._read_tabs({"onglet"}, "main"))
        self.assertEqual(self.sent, [])
        tabs["onglet"] = f"https://{SITE}/facture/2026-05"
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=self.send):
            self.assertTrue(self.visit()._read_tabs({"onglet"}, "main"))
        self.assertEqual([request.url for request in self.sent], [f"https://{SITE}/facture/2026-05"])


class MaskTests(SimpleTestCase):
    def test_every_form_of_a_secret_is_masked(self):
        page = " | ".join(SECRET_FORMS)
        masked = mask_secrets(page, [SECRET])
        for form in SECRET_FORMS:
            self.assertNotIn(form, masked)
        self.assertEqual(masked, " | ".join([website.MASK] * len(SECRET_FORMS)))

    def test_nothing_typed_masks_nothing(self):
        self.assertEqual(mask_secrets("<p>Bonjour</p>", []), "<p>Bonjour</p>")
        self.assertEqual(mask_secrets("<p>Bonjour</p>", [""]), "<p>Bonjour</p>")
        self.assertEqual(mask_secrets("<p>Bonjour</p>", [""], logins=[""]), "<p>Bonjour</p>")
        self.assertFalse(holds_a_secret("<p>Bonjour</p>", [""], logins=[""]))

    def test_the_login_is_masked_whatever_its_case(self):
        """A site prints it back as it stores it: lowercased, in capitals,
        each word capitalised - in every form a page writes it."""
        login = "Jean.Dupont@Exemple.invalid"
        page = (
            "jean.dupont@exemple.invalid | JEAN.DUPONT@EXEMPLE.INVALID | Jean.Dupont@exemple.Invalid | "
            "jean.dupont%40exemple.invalid | JEAN.DUPONT%40EXEMPLE.INVALID | {&quot;u&quot;: &quot;JEAN.DUPONT@"
            "EXEMPLE.INVALID&quot;}"
        )
        masked = mask_secrets(page, [SECRET], logins=[login])
        self.assertNotIn("jean.dupont", masked.casefold())
        self.assertEqual(masked.count(website.MASK), 6)
        self.assertFalse(holds_a_secret(masked, [SECRET], logins=[login]))
        self.assertTrue(holds_a_secret("Bonjour JEAN.DUPONT@EXEMPLE.INVALID", [SECRET], logins=[login]))

    def test_a_login_printed_in_capitals_by_a_rule_of_its_language_is_masked(self):
        """« ß » has no capital of its own: « STRASSE » is how it prints."""
        masked = mask_secrets("Compte STRASSE@EXEMPLE.INVALID", [], logins=["straße@exemple.invalid"])
        self.assertEqual(masked, f"Compte {website.MASK}")

    def test_an_accented_login_is_masked_whatever_its_case_inside_json_too(self):
        r"""Written É in capitals, é in small letters: one more
        spelling, not one more case."""
        masked = mask_secrets('{"u": "J\\u00c9R\\u00d4ME"} JÉRÔME', [], logins=["Jérôme"])
        self.assertEqual(masked, f'{{"u": "{website.MASK}"}} {website.MASK}')

    def test_a_password_is_masked_in_its_own_case_only(self):
        """Its case is part of it: « PA"SS… » is another password - and
        matched whatever its case, a short one would mask any word that
        happens to spell it."""
        shouted = SECRET.upper()
        self.assertEqual(mask_secrets(f"{SECRET} {shouted}", [SECRET], logins=[LOGIN]), f"{website.MASK} {shouted}")
        self.assertFalse(holds_a_secret(shouted, [SECRET], logins=[LOGIN]))
        self.assertTrue(holds_a_secret(f"x{SECRET}x", [SECRET]))

    def test_a_bare_address(self):
        self.assertEqual(bare_url("https://box.exemple.invalid/a/b?pwd=x#y"), "https://box.exemple.invalid/a/b")
        self.assertEqual(
            bare_url("https://moi:secret@box.exemple.invalid:8443/a"), "https://box.exemple.invalid:8443/a"
        )
        self.assertEqual(bare_url(""), "")


# --------------------------------------------------------------- keeping


class DumpRetentionTests(SimpleTestCase):
    """What failures and « Tester » keep, kept a while: 14 days, 20 files a
    folder - and a « Tester »'s own folder, gone after 14 days."""

    DAY = 86400

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = time.time()

    def file(self, path, days_ago, content=b"x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        moment = self.now - days_ago * self.DAY
        os.utime(path, (moment, moment))
        return path

    def folder(self, name, days_ago):
        path = os.path.join(self.root, name)
        self.file(os.path.join(path, website.DEBUG_DIR, "page.html"), days_ago)
        moment = self.now - days_ago * self.DAY
        os.utime(path, (moment, moment))
        return path

    def test_old_dumps_go_and_the_twenty_newest_stay(self):
        here = os.path.join(self.root, "type-3")
        debug = os.path.join(here, website.DEBUG_DIR)
        recent = [self.file(os.path.join(debug, f"r{n:02}.png"), days_ago=n / 10) for n in range(24)]
        self.file(os.path.join(debug, "vieux.html"), days_ago=15)
        invoice = self.file(os.path.join(here, "facture.pdf"), days_ago=30)
        website.prune_dumps(here, now=self.now)
        self.assertEqual(sorted(os.listdir(debug)), sorted(os.path.basename(path) for path in recent[:20]))
        self.assertTrue(os.path.exists(invoice))

    def test_old_test_folders_beside_it_go_and_nothing_else(self):
        here = os.path.join(self.root, "type-3")
        self.file(os.path.join(here, website.DEBUG_DIR, "page.html"), days_ago=0)
        gone = self.folder("test-12", days_ago=20)
        kept = [
            self.folder("test-13", days_ago=1),
            self.folder("metro", days_ago=30),
            self.folder("type-4", days_ago=30),
            self.folder("test-x", days_ago=30),
            self.folder("test-12a", days_ago=30),
            self.folder("tests-1", days_ago=30),
            self.file(os.path.join(self.root, "test-14"), days_ago=30),
        ]
        website.prune_dumps(here, now=self.now)
        self.assertFalse(os.path.exists(gone))
        for path in kept:
            self.assertTrue(os.path.exists(path), path)

    def test_the_runs_own_folder_is_never_taken(self):
        here = self.folder("test-15", days_ago=30)
        website.prune_dumps(here, now=self.now)
        self.assertTrue(os.path.isdir(here))

    def test_the_old_pages_kept_for_the_other_sources_go_too(self):
        """A source deleted, or never failing again, kept its pages for
        good: only its own run ever pruned them."""
        here = os.path.join(self.root, "type-3")
        other = os.path.join(self.root, "type-4", website.DEBUG_DIR)
        old = [
            self.file(os.path.join(other, "20260901-101010.png"), days_ago=15),
            self.file(os.path.join(other, "20260901-101010.html"), days_ago=15),
        ]
        recent = self.file(os.path.join(other, "20260930-101010.html"), days_ago=1)
        invoice = self.file(os.path.join(self.root, "type-4", "facture.pdf"), days_ago=30)
        website.prune_dumps(here, now=self.now)
        for path in old:
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(invoice))

    def test_only_kept_pages_go_from_the_other_sources_folders(self):
        """Beside the run's own folder, a screenshot or a page - nothing
        else a _debug folder might hold."""
        here = os.path.join(self.root, "type-3")
        other = os.path.join(self.root, "type-4", website.DEBUG_DIR)
        kept = self.file(os.path.join(other, "notes.txt"), days_ago=30)
        website.prune_dumps(here, now=self.now)
        self.assertTrue(os.path.exists(kept))

    def test_every_visit_starts_by_pruning(self):
        """Not only once a page is kept: a source refused before any page
        is read - a run every day - prunes all the same."""
        here = os.path.join(self.root, "type-5")
        old_own = self.file(os.path.join(here, website.DEBUG_DIR, "20260901-101010.png"), days_ago=15)
        old_other = self.file(os.path.join(self.root, "type-4", website.DEBUG_DIR, "20260901-101010.html"), days_ago=15)
        old_test = self.folder("test-12", days_ago=20)
        recent = self.file(os.path.join(self.root, "type-4", website.DEBUG_DIR, "20260930-101010.png"), days_ago=1)
        with self.assertRaises(WebsiteError):
            # Refused before any browser starts: an http login page.
            fetch_website_invoices(
                recipe(login_url=f"http://{SITE}/login"),
                here,
                date(2026, 5, 1),
                date(2026, 5, 31),
                log=lambda message: None,
                driver_factory=lambda *args: FakeBrowser(),
            )
        for path in (old_own, old_other, old_test):
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.exists(recent))

    def test_a_tester_run_starts_by_pruning_too(self):
        here = os.path.join(self.root, "test-16")
        old_other = self.file(os.path.join(self.root, "type-4", website.DEBUG_DIR, "20260901-101010.png"), days_ago=15)
        with self.assertRaises(WebsiteError):
            website.list_website_invoices(
                recipe(login_url=f"http://{SITE}/login"),
                here,
                date(2026, 5, 1),
                date(2026, 5, 31),
                log=lambda message: None,
                driver_factory=lambda *args: FakeBrowser(),
            )
        self.assertFalse(os.path.exists(old_other))

    def test_a_dump_written_clears_what_is_past_keeping(self):
        here = os.path.join(self.root, "type-3")
        old = self.file(os.path.join(here, website.DEBUG_DIR, "vieux.png"), days_ago=15)
        logged = []
        with mock.patch.object(website, "time", FAST):
            visit = website._Visit(recipe(), here, logged.append, None, lambda *args: FakeBrowser(), True)
            visit.diagnose("Box Exemple : échec")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(
            sorted(name.rsplit(".", 1)[1] for name in os.listdir(os.path.join(here, website.DEBUG_DIR))),
            ["html", "png"],
            logged,
        )
