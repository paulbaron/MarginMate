"""Where a customer portal's password goes, and who decides it (security
review of 01/10/2026).

A portal's source names the page it signs in on and the two names its login
and password are kept under on « Identifiants »; the gather, and « Tester »
at once, type what is kept under those names into that page. So:

- its login page, and its invoices page when one is given, are https
  addresses (`models.portal_host`) - a password is never typed on a page
  sent in clear, and the store binds a password to that https host;
- its two names are its own: never one name for both, never a name another
  source uses in the other role (« Identifiants » shows a login in clear),
  never a name another SITE's source uses (one site's password would be
  typed into the other's page) - one site's sources share theirs;
- the names derived from the site (`forms.portal_env_names`) carry a digest
  of the exact host, so two sites whose names fold alike never share one;
- only the espace's owner, his MarginMate password confirmed
  (accounts/sudo.py), saves or tests one.

The form and the « Données » import both run `WebsiteInvoiceSource.clean`
(the import's side: transfer/tests/test_sources_section.py).

Every address and name below is invented, and nothing signs in anywhere:
« Tester »'s thread is replaced before it could start.
"""

import time
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from accounts import sudo
from accounts.models import Membership
from accounts.tenancy import current_tenant
from invoices.forms import WebsiteInvoiceSourceForm, portal_env_names
from invoices.models import (
    ENV_NAME_RE,
    INVOICES_URL_NOT_HTTPS,
    LOGIN_URL_NOT_HTTPS,
    SAME_NAME_TWICE,
    EmailInvoiceSource,
    InvoiceType,
    ScrapeJob,
    WebsiteInvoiceSource,
    app_env_name,
)
from invoices.views import PORTAL_OWNER_ONLY
from tests.factories import make_invoice_type, make_supplier
from tests.runner import test_user
from tests.support import NoNetworkTestCase

WEBSITE = InvoiceType.SourceKind.WEBSITE
CREATE = reverse("invoices:invoice_type_create")
BOX_URL = "https://espace-client.box-exemple.invalid/connexion"
WATER_URL = "https://eau-exemple.invalid/connexion"


def confirm_password(client, user=None, seconds=600):
    """The test client logged in as the test espace's owner (or `user`), his
    MarginMate password confirmed as accounts/sudo.py records it - what a
    portal's save or « Tester » asks for. Logged in first: a session made
    before the client's first request would leave it anonymous
    (tests.runner.TenantClient logs in only a client carrying none)."""
    user = user or test_user()
    client.force_login(user)
    session = client.session
    session[sudo.SESSION_KEY] = {"user": user.pk, "until": time.time() + seconds}
    session.save()
    return user


def portal(name, url=BOX_URL, login="BOX_LOGIN", password="BOX_PASSWORD", **settings) -> WebsiteInvoiceSource:
    """A portal source as the database may hold it - objects.create, not the
    form: these tests set up the other sources a new one is judged against."""
    invoice_type = InvoiceType.objects.create(
        supplier=make_supplier(name=f"Fournisseur {name}"), name=name, source_kind=WEBSITE
    )
    return WebsiteInvoiceSource.objects.create(
        invoice_type=invoice_type, login_url=url, username_env=login, password_env=password, **settings
    )


class ModelRuleTests(TestCase):
    """`WebsiteInvoiceSource.clean`, as `full_clean` runs it (the form's
    validation and the import's both do)."""

    @staticmethod
    def errors(source=None, **settings) -> dict[str, list[str]]:
        if source is None:
            source = WebsiteInvoiceSource(
                **{"login_url": WATER_URL, "username_env": "EAU_LOGIN", "password_env": "EAU_PASSWORD", **settings}
            )
        else:
            for name, value in settings.items():
                setattr(source, name, value)
        try:
            source.full_clean(exclude=["invoice_type"])
        except ValidationError as exc:
            return exc.message_dict
        return {}

    def test_an_https_portal_with_names_of_its_own_passes(self):
        self.assertEqual(self.errors(), {})

    def test_a_login_page_in_clear_is_refused(self):
        self.assertEqual(
            self.errors(login_url="http://eau-exemple.invalid/connexion"), {"login_url": [LOGIN_URL_NOT_HTTPS]}
        )

    def test_the_test_settings_local_portal_only_is_allowed_in_clear(self):
        """config/settings_test.py's PORTAL_PLAIN_HTTP_HOSTS (a browser test's
        portal on this machine); no production setting names a host."""
        self.assertEqual(self.errors(login_url="http://127.0.0.1:8123/connexion"), {})
        with override_settings(PORTAL_PLAIN_HTTP_HOSTS=frozenset()):
            self.assertIn("login_url", self.errors(login_url="http://127.0.0.1:8123/connexion"))

    def test_an_invoices_page_in_clear_is_refused_and_blank_is_fine(self):
        self.assertEqual(
            self.errors(invoices_url="http://eau-exemple.invalid/factures"),
            {"invoices_url": [INVOICES_URL_NOT_HTTPS]},
        )
        self.assertEqual(self.errors(invoices_url="https://eau-exemple.invalid/factures"), {})
        self.assertEqual(self.errors(invoices_url=""), {})

    def test_one_name_for_the_login_and_the_password_is_refused(self):
        self.assertEqual(
            self.errors(username_env="EAU_COMPTE", password_env="EAU_COMPTE"), {"password_env": [SAME_NAME_TWICE]}
        )

    def test_another_sources_password_is_never_a_login_here(self):
        """« Identifiants » shows a login in clear: the Box's password would be
        printed there as this source's login - whatever the site."""
        portal("Box - Factures")
        for url in (BOX_URL, WATER_URL):
            with self.subTest(url=url):
                errors = self.errors(login_url=url, username_env="BOX_PASSWORD", password_env="EAU_PASSWORD")
                self.assertEqual(list(errors), ["username_env"])
                self.assertIn("Ce nom est celui du mot de passe de « Box - Factures »", errors["username_env"][0])

    def test_another_sources_login_is_never_a_password_here(self):
        portal("Box - Factures")
        errors = self.errors(login_url=BOX_URL, username_env="EAU_LOGIN", password_env="BOX_LOGIN")
        self.assertEqual(list(errors), ["password_env"])
        self.assertIn("Ce nom est celui de l'identifiant de « Box - Factures »", errors["password_env"][0])

    def test_the_names_of_another_sites_source_are_refused(self):
        """Their password, typed on « Identifiants » for the Box's site,
        would be typed into this one's page."""
        portal("Box - Factures")
        errors = self.errors(username_env="BOX_LOGIN", password_env="BOX_PASSWORD")
        self.assertEqual(
            errors,
            {
                "password_env": [
                    (
                        "Ces noms sont déjà ceux de « Box - Factures » (site espace-client.box-exemple.invalid) : "
                        "son mot de passe serait tapé sur ce site."
                    )
                ]
            },
        )

    def test_either_name_of_another_sites_source_is_refused(self):
        portal("Box - Factures")
        password = self.errors(username_env="EAU_LOGIN", password_env="BOX_PASSWORD")
        self.assertEqual(list(password), ["password_env"])
        self.assertIn("celui du mot de passe de « Box - Factures » (site", password["password_env"][0])
        self.assertIn("son mot de passe serait tapé sur ce site", password["password_env"][0])
        login = self.errors(username_env="BOX_LOGIN", password_env="EAU_PASSWORD")
        self.assertEqual(list(login), ["username_env"])
        self.assertIn("celui de l'identifiant de « Box - Factures » (site", login["username_env"][0])

    def test_a_paused_source_or_a_mailbox_sources_unused_portal_counts_too(self):
        """Switched back on, either signs in with those names again."""
        paused = portal("Box - Factures")
        InvoiceType.objects.filter(pk=paused.invoice_type_id).update(is_active=False)
        mailbox = portal("Mobile - Factures", login="MOBILE_LOGIN", password="MOBILE_PASSWORD")
        InvoiceType.objects.filter(pk=mailbox.invoice_type_id).update(source_kind=InvoiceType.SourceKind.EMAIL)
        self.assertIn("password_env", self.errors(username_env="BOX_LOGIN", password_env="BOX_PASSWORD"))
        self.assertIn("password_env", self.errors(username_env="MOBILE_LOGIN", password_env="MOBILE_PASSWORD"))

    def test_two_sources_of_one_site_share_their_names(self):
        """One account, typed once - the same host, whatever the page."""
        portal("Box - Factures")
        self.assertEqual(
            self.errors(
                login_url="https://espace-client.box-exemple.invalid/autre-page",
                username_env="BOX_LOGIN",
                password_env="BOX_PASSWORD",
            ),
            {},
        )

    def test_a_source_is_never_its_own_other(self):
        """Saved again, or moved to another site while its names stay: no
        other source has them."""
        box = portal("Box - Factures")
        self.assertEqual(self.errors(box), {})
        self.assertEqual(self.errors(box, login_url=WATER_URL), {})
        # Nor is the portal row an import is about to rewrite, made anew for
        # the same type.
        probe = WebsiteInvoiceSource(
            invoice_type_id=box.invoice_type_id, login_url=WATER_URL, username_env="BOX_LOGIN", password_env="X_PWD"
        )
        self.assertEqual(self.errors(probe), {})

    def test_a_login_page_refused_says_nothing_about_another_site(self):
        """Its site is unknown: the address is what to correct first."""
        portal("Box - Factures")
        errors = self.errors(login_url="http://eau-exemple.invalid/", username_env="BOX_LOGIN")
        self.assertEqual(list(errors), ["login_url"])

    def test_an_address_the_form_cannot_read_is_said_alone(self):
        portal("Box - Factures")
        form = WebsiteInvoiceSourceForm(
            {
                "site-login_url": "pas une adresse",
                "site-username_env": "BOX_LOGIN",
                "site-password_env": "BOX_PASSWORD",
            },
            prefix="site",
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(list(form.errors), ["login_url"])

    def test_the_form_says_it_on_the_field(self):
        portal("Box - Factures")
        form = WebsiteInvoiceSourceForm(
            {
                "site-login_url": "http://eau-exemple.invalid/connexion",
                "site-username_env": "box_password",
                "site-password_env": "EAU_PASSWORD",
            },
            prefix="site",
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["login_url"], [LOGIN_URL_NOT_HTTPS])
        self.assertIn("celui du mot de passe de « Box - Factures »", form.errors["username_env"][0])


class DerivedNameTests(SimpleTestCase):
    """`forms.portal_env_names`: the names a portal's credentials are kept
    under when none was typed. Readable, and the exact host's own."""

    def test_a_readable_slug_and_the_hosts_digest(self):
        self.assertEqual(
            portal_env_names(BOX_URL),
            (
                "PORTAL_ESPACE_CLIENT_BOX_EXEMPLE_INVALID_AFAA7D_LOGIN",
                "PORTAL_ESPACE_CLIENT_BOX_EXEMPLE_INVALID_AFAA7D_PASSWORD",
            ),
        )

    def test_two_hosts_folding_alike_never_share_an_account(self):
        """The slug turns « - » and « . » into « _ » alike, drops « www. »
        and cuts at 40 characters: without the digest, each pair below was
        one account, one site's password typed on the other."""
        for one, other in (
            ("https://mon-espace.exemple.invalid/", "https://mon.espace.exemple.invalid/"),
            ("https://www.eau-exemple.invalid/", "https://eau-exemple.invalid/"),
            (
                "https://factures-et-abonnements-du-fournisseur-principal.exemple.invalid/",
                "https://factures-et-abonnements-du-fournisseur-principal.autre.invalid/",
            ),
        ):
            with self.subTest(one=one):
                self.assertNotEqual(portal_env_names(one)[0], portal_env_names(other)[0])
                self.assertNotEqual(portal_env_names(one)[1], portal_env_names(other)[1])
        self.assertEqual(
            portal_env_names("https://mon-espace.exemple.invalid/")[0], "PORTAL_MON_ESPACE_EXEMPLE_INVALID_5C5740_LOGIN"
        )
        self.assertEqual(
            portal_env_names("https://mon.espace.exemple.invalid/")[0], "PORTAL_MON_ESPACE_EXEMPLE_INVALID_8663E6_LOGIN"
        )

    def test_one_site_gives_one_pair_whatever_the_page(self):
        """Two sources of one site share their account; a host's case and a
        trailing dot are the same host (as `portal_host` binds it)."""
        names = portal_env_names(BOX_URL)
        for url in (
            "https://espace-client.box-exemple.invalid/factures?mois=05",
            "https://ESPACE-CLIENT.Box-Exemple.invalid/connexion",
            "https://espace-client.box-exemple.invalid./connexion",
            "https://espace-client.box-exemple.invalid:443/connexion",
        ):
            with self.subTest(url=url):
                self.assertEqual(portal_env_names(url), names)

    def test_every_derived_name_is_a_name_the_store_and_the_column_take(self):
        """Upper-case letters, digits and « _ », at most 64 characters (the
        column and `vault.allowed_name`), never the application's own, the
        login and the password apart."""
        long_host = ".".join(["abcdefghij" * 6] * 4) + ".invalid"
        for url in (BOX_URL, f"https://{long_host}/", "https://123.exemple.invalid/", "https://metro.exemple.invalid/"):
            with self.subTest(url=url):
                login, password = portal_env_names(url)
                for name in (login, password):
                    self.assertRegex(name, ENV_NAME_RE)
                    self.assertLessEqual(len(name), 64)
                    self.assertFalse(app_env_name(name))
                self.assertNotEqual(login, password)

    def test_an_address_with_no_host_still_gives_names(self):
        """The form refuses the address itself; deriving never raises."""
        for url in ("", "pas une adresse", "https://[::1"):
            with self.subTest(url=url):
                login, password = portal_env_names(url)
                self.assertTrue(login.startswith("PORTAL_SITE_"))
                self.assertTrue(password.endswith("_PASSWORD"))


class OwnerOnlyTests(NoNetworkTestCase):
    """Saving or testing a portal's source decides where a stored password
    is typed: the espace's owner's, his MarginMate password confirmed. The
    page itself shows no secret, and a mailbox source is not held back."""

    def setUp(self):
        super().setUp()
        self.supplier = make_supplier(code="BOX_X", name="Box Exemple", parser_key="", expenses_only=True)
        self.user = test_user()
        self.client.force_login(self.user)

    def confirm(self, seconds=600):
        confirm_password(self.client, self.user, seconds)

    def make_a_member(self):
        Membership.objects.filter(user=self.user, tenant=current_tenant()).update(role=Membership.Role.MEMBER)

    def post_portal(self, url=CREATE, **fields):
        data = {
            "name": "Box Exemple - Factures",
            "supplier": self.supplier.pk,
            "source_kind": "WEBSITE",
            "parser_key": "",
            "is_active": "on",
            "action": "save",
            "site-login_url": BOX_URL,
            "site-username_env": "BOX_LOGIN",
            "site-password_env": "BOX_PASSWORD",
        }
        data.update(fields)
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(url, data)
        return response, thread

    def assertNothingStarted(self, thread):
        thread.assert_not_called()
        self.assertFalse(ScrapeJob.objects.exists())

    # -- a member, his password confirmed or not ------------------------------------
    def test_a_member_cannot_save_a_new_portal(self):
        self.make_a_member()
        self.confirm()
        response, thread = self.post_portal()
        self.assertContains(response, escape(PORTAL_OWNER_ONLY), status_code=403)
        self.assertFalse(InvoiceType.objects.filter(name="Box Exemple - Factures").exists())
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertNothingStarted(thread)

    def test_a_member_cannot_test_a_portal(self):
        self.make_a_member()
        self.confirm()
        response, thread = self.post_portal(action="test", test_start_date="2026-04-01", test_end_date="2026-05-31")
        self.assertContains(response, escape(PORTAL_OWNER_ONLY), status_code=403)
        self.assertNothingStarted(thread)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())

    def test_a_member_cannot_change_a_saved_portal(self):
        box = portal("Box Exemple - Factures")
        self.make_a_member()
        self.confirm()
        url = reverse("invoices:invoice_type_update", args=[box.invoice_type_id])
        for action in ("save", "test"):
            with self.subTest(action=action):
                response, thread = self.post_portal(url, action=action, **{"site-login_url": WATER_URL})
                self.assertEqual(response.status_code, 403)
                self.assertNothingStarted(thread)
        box.refresh_from_db()
        self.assertEqual(box.login_url, BOX_URL)
        # The page drawn back is the source as saved, beside the reason.
        self.assertContains(response, f'value="{BOX_URL}"', status_code=403)

    def test_a_member_cannot_turn_a_mailbox_source_into_a_portal(self):
        mailbox = make_invoice_type(supplier=self.supplier, name="Box Exemple - Factures", sender_pattern="box@")
        self.make_a_member()
        self.confirm()
        response, thread = self.post_portal(reverse("invoices:invoice_type_update", args=[mailbox.pk]))
        self.assertEqual(response.status_code, 403)
        mailbox.refresh_from_db()
        self.assertEqual(mailbox.source_kind, InvoiceType.SourceKind.EMAIL)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertNothingStarted(thread)

    def test_a_member_still_sees_the_page_and_keeps_the_mailbox_sources(self):
        """The mailbox's server and account are fixed on « Identifiants »:
        a mailbox source's patterns send no password anywhere new."""
        box = portal("Box Exemple - Factures")
        self.make_a_member()
        page = self.client.get(reverse("invoices:invoice_type_update", args=[box.invoice_type_id]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'value="{BOX_URL}"')
        response = self.client.post(
            CREATE,
            {
                "name": "Grossiste - Factures",
                "supplier": self.supplier.pk,
                "source_kind": "EMAIL",
                "parser_key": "",
                "is_active": "on",
                "action": "save",
                "sender_pattern": "factures@",
                "attachment_pattern": r"(?i)\.pdf$",
            },
        )
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        self.assertTrue(EmailInvoiceSource.objects.filter(invoice_type__name="Grossiste - Factures").exists())

    # -- the owner, his password not confirmed --------------------------------------
    def test_the_owner_is_asked_his_password_before_saving(self):
        response, thread = self.post_portal()
        self.assertRedirects(response, sudo.confirm_url(CREATE), fetch_redirect_response=False)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertFalse(InvoiceType.objects.filter(name="Box Exemple - Factures").exists())
        self.assertNothingStarted(thread)

    def test_the_owner_is_asked_his_password_before_testing(self):
        response, thread = self.post_portal(action="test")
        self.assertRedirects(response, sudo.confirm_url(CREATE), fetch_redirect_response=False)
        self.assertNothingStarted(thread)

    def test_the_confirmation_comes_back_to_the_page_it_was_asked_from(self):
        box = portal("Box Exemple - Factures")
        url = reverse("invoices:invoice_type_update", args=[box.invoice_type_id]) + "?retour=/invoices/types/"
        response, thread = self.post_portal(url, **{"site-login_url": WATER_URL})
        self.assertRedirects(response, sudo.confirm_url(url), fetch_redirect_response=False)
        box.refresh_from_db()
        self.assertEqual(box.login_url, BOX_URL)
        self.assertNothingStarted(thread)

    def test_a_confirmation_that_ended_is_asked_again(self):
        self.confirm(seconds=-1)
        response, _thread = self.post_portal()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:confirm_password")))
        self.assertFalse(WebsiteInvoiceSource.objects.exists())

    # -- the owner, confirmed ------------------------------------------------------
    def test_once_confirmed_the_owner_saves_a_portal(self):
        self.confirm()
        response, thread = self.post_portal()
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        site = WebsiteInvoiceSource.objects.get()
        self.assertEqual((site.login_url, site.username_env, site.password_env), (BOX_URL, "BOX_LOGIN", "BOX_PASSWORD"))
        self.assertNothingStarted(thread)

    def test_once_confirmed_the_owner_tests_a_portal_and_the_confirmation_is_kept_alive(self):
        self.confirm(seconds=60)
        before = self.client.session[sudo.SESSION_KEY]["until"]
        response, thread = self.post_portal(action="test")
        self.assertEqual(response.status_code, 200)
        thread.assert_called_once()
        self.assertEqual(response.context["test_job"], ScrapeJob.objects.get(kind=ScrapeJob.Kind.TEST))
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertGreater(self.client.session[sudo.SESSION_KEY]["until"], before + 300)

    def test_a_portal_in_clear_is_refused_to_the_owner_too(self):
        self.confirm()
        for action in ("save", "test"):
            with self.subTest(action=action):
                response, thread = self.post_portal(action=action, **{"site-login_url": "http://box.exemple.invalid/"})
                self.assertContains(response, escape(LOGIN_URL_NOT_HTTPS))
                self.assertNothingStarted(thread)
        self.assertFalse(WebsiteInvoiceSource.objects.exists())

    def test_another_sites_names_are_refused_to_the_owner_and_tested_nowhere(self):
        portal("Eau - Factures", url=WATER_URL, login="EAU_LOGIN", password="EAU_PASSWORD")
        self.confirm()
        for action in ("save", "test"):
            with self.subTest(action=action):
                response, thread = self.post_portal(
                    action=action, **{"site-username_env": "EAU_LOGIN", "site-password_env": "EAU_PASSWORD"}
                )
                self.assertContains(response, "Ces noms sont déjà ceux de « Eau - Factures »")
                self.assertNothingStarted(thread)
        self.assertEqual(WebsiteInvoiceSource.objects.count(), 1)

    def test_names_left_blank_are_the_sites_own(self):
        self.confirm()
        response, _thread = self.post_portal(**{"site-username_env": "", "site-password_env": ""})
        self.assertRedirects(response, reverse("invoices:invoice_type_list"))
        site = WebsiteInvoiceSource.objects.get()
        self.assertEqual((site.username_env, site.password_env), portal_env_names(BOX_URL))
