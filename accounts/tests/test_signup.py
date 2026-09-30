"""« Créer votre espace » (accounts/pages.py, accounts/signup.py), in multi
mode for real: a valid invitation makes a login, a tenant and its
membership, all or nothing; the code is looked at only once everything else
is valid, and a used, expired or unknown one reads the same; failures are
counted. Names and addresses invented."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import invitations, limiter, paths, provisioning, signup
from accounts.models import Invitation, Membership, Tenant
from accounts.router import ACCOUNTS_ALIAS
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase
from invoices.models import InvoiceType, Supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

SIGNUP = reverse("accounts:signup")
PASSWORD = "Tabouret-Zinc-47"


class SignupTestCase(TenancyTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.invitation, self.code = invitations.create_invitation(note="Bar de la gare (essai)")

    def form(self, **changes):
        data = {
            "code": self.code,
            "bar_name": "Le Zinc d'Essai",
            "email": "Gerante@Example.invalid",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        data.update(changes)
        return data

    def post(self, client=None, ip="192.0.2.20", **changes):
        return (client or self.client).post(SIGNUP, self.form(**changes), REMOTE_ADDR=ip)

    def assertNothingMade(self):
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Tenant.objects.exists())
        self.assertFalse(Membership.objects.exists())
        self.assertEqual([p.name for p in paths.tenants_root().iterdir()], [paths.TEMPLATE_DIR])

    def assertUnused(self, invitation=None):
        invitation = invitation or self.invitation
        invitation.refresh_from_db()
        self.assertIsNone(invitation.used_at)
        self.assertIsNone(invitation.used_by)


class SignupTests(SignupTestCase):
    def test_the_page_asks_what_the_spec_says_in_french(self):
        response = self.client.get(SIGNUP)
        self.assertEqual(response.status_code, 200)
        for words in ("Créer votre espace", "Code d'invitation", "Nom du bar", "Adresse e-mail", "Mot de passe"):
            self.assertContains(response, words.replace("'", "&#x27;") if "'" in words else words)
        # Django's own help, in French - as sentences: its HTML list inside
        # the help's <p> came out of it, drawn as a list of body text.
        self.assertContains(response, "au minimum 8 caractères")
        self.assertNotContains(response, "<li>")
        self.assertNotContains(response, "<nav")
        assertNoUnrenderedTemplateSyntax(self, response, SIGNUP)

    def test_a_valid_signup_makes_the_login_the_tenant_and_the_membership_then_logs_in(self):
        response = self.post()
        self.assertRedirects(response, "/", fetch_redirect_response=False)

        user = get_user_model().objects.get()
        self.assertEqual(user.username, "gerante@example.invalid")
        self.assertEqual(user.email, "gerante@example.invalid")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

        tenant = Tenant.objects.get()
        self.assertEqual(tenant.name, "Le Zinc d'Essai")
        self.assertRegex(tenant.dir_name, r"^[a-z0-9]{12}$")
        self.assertNotIn("zinc", tenant.dir_name)
        self.assertFalse(tenant.uses_server_integrations)
        self.assertTrue(tenant.is_active)
        self.assertTrue(paths.tenant_database(tenant).is_file())
        for name in paths.FOLDERS:
            self.assertTrue((paths.tenant_dir(tenant) / name).is_dir(), name)

        membership = Membership.objects.get()
        self.assertEqual((membership.user, membership.tenant, membership.role), (user, tenant, Membership.Role.OWNER))

        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.used_at)
        self.assertEqual(self.invitation.used_by, user)

        # Logged in, in his new tenant: its name in the topbar, the owner's
        # integrations off in it.
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        home = self.client.get("/", follow=True)
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Bienvenue : l&#x27;espace « Le Zinc d&#x27;Essai » est prêt.")
        self.assertContains(home, 'class="topbar-tenant"')
        with bound_tenant(tenant):
            self.assertFalse(Supplier.objects.get(code="METRO").is_scrapable)
            self.assertFalse(InvoiceType.objects.filter(source_kind="EMAIL", is_active=True).exists())

    def test_the_code_is_read_whatever_its_case_dashes_or_spaces(self):
        typed = "  " + self.code.replace("-", " ").lower() + " "
        self.assertRedirects(self.post(code=typed), "/", fetch_redirect_response=False)

    def test_a_used_an_expired_and_an_unknown_code_read_the_same(self):
        used, used_code = invitations.create_invitation()
        Invitation.objects.filter(pk=used.pk).update(used_at=timezone.now())
        expired, expired_code = invitations.create_invitation(days=1)
        Invitation.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        for code in (used_code, expired_code, "ABCD-EFGH-JKLM-NPQR-STUV-WXYZ", "0"):
            with self.subTest(code=code):
                response = self.post(code=code)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Ce code d&#x27;invitation n&#x27;ouvre pas d&#x27;inscription")
                self.assertNothingMade()
                self.assertNotIn("_auth_user_id", self.client.session)
        self.assertUnused()

    def test_nothing_is_said_about_the_code_while_the_rest_is_invalid(self):
        """« Code inconnu » beside a password too short would answer, for
        free, whether a code exists: the code is looked at last."""
        for code in ("ABCD-EFGH-JKLM-NPQR-STUV-WXYZ", self.code):
            with self.subTest(code=code == self.code):
                response = self.post(code=code, password1="court", password2="court")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Ce mot de passe est trop court")
                self.assertNotContains(response, "ouvre pas d&#x27;inscription")
                self.assertNothingMade()
        self.assertUnused()
        # Nor is any of it counted as a guess: past ten of them, the right
        # form still goes through.
        for _ in range(limiter.LIMIT):
            self.post(code="ABCD-EFGH-JKLM-NPQR-STUV-WXYZ", password1="court", password2="court")
        self.assertRedirects(self.post(), "/", fetch_redirect_response=False)

    def test_the_form_s_own_refusals(self):
        cases = {
            "Les deux mots de passe ne sont pas identiques.": {"password2": PASSWORD + "x"},
            "Saisissez une adresse e-mail valide.": {"email": "pas-une-adresse"},
            "Ce champ est obligatoire.": {"bar_name": "   "},
            "Ce mot de passe est entièrement numérique.": {"password1": "8412953760", "password2": "8412953760"},
        }
        for message, changes in cases.items():
            with self.subTest(message=message):
                response = self.post(**changes)
                self.assertContains(response, message)
                self.assertNothingMade()
        self.assertUnused()

    def test_an_address_that_has_a_login_is_refused_and_keeps_the_code(self):
        get_user_model().objects.create_user(username="deja", email="gerante@example.invalid", password=PASSWORD)
        response = self.post()
        # Refused as a code that opens nothing is, word for word: the page
        # never says the address has an account (security audit ANON-5,
        # accounts/tests/test_signup_neutral.py).
        self.assertContains(response, "Ce code d&#x27;invitation n&#x27;ouvre pas d&#x27;inscription")
        self.assertNotContains(response, "existe déjà")
        self.assertFalse(Tenant.objects.exists())
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertUnused()

    def test_logged_in_the_page_sends_home(self):
        tenant = self.make_tenant("Bar Alpha")
        self.client.force_login(self.make_member(tenant))
        self.assertRedirects(self.client.get(SIGNUP), "/", fetch_redirect_response=False)
        self.assertRedirects(self.post(), "/", fetch_redirect_response=False)
        self.assertUnused()
        self.assertEqual(Tenant.objects.count(), 1)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(client=client).status_code, 403)
        self.assertNothingMade()


class SignupAllOrNothingTests(SignupTestCase):
    def test_a_failure_after_the_tenant_exists_leaves_nothing(self):
        with mock.patch.object(Membership.objects, "create", side_effect=RuntimeError("panne d'essai")):
            with self.assertRaisesMessage(RuntimeError, "panne d'essai"):
                signup.sign_up(code=self.code, bar_name="Bar Raté", email="rate@example.invalid", password=PASSWORD)
        self.assertNothingMade()
        self.assertUnused()

    def test_a_failure_while_the_tenant_is_made_leaves_nothing(self):
        with mock.patch.object(provisioning, "_migrate_bound", side_effect=RuntimeError("panne d'essai")):
            with self.assertRaisesMessage(RuntimeError, "panne d'essai"):
                signup.sign_up(code=self.code, bar_name="Bar Raté", email="rate@example.invalid", password=PASSWORD)
        self.assertNothingMade()
        self.assertUnused()

    def test_a_code_taken_in_between_is_refused(self):
        """Two signups racing on one code: the second finds it usable, then
        its UPDATE finds it used - refused, nothing made."""
        stale = Invitation.objects.get(pk=self.invitation.pk)
        Invitation.objects.filter(pk=stale.pk).update(used_at=timezone.now())
        with mock.patch.object(invitations, "usable_invitation", return_value=stale):
            with self.assertRaises(signup.SignupRefused) as refused:
                signup.sign_up(code=self.code, bar_name="Bar Second", email="second@example.invalid", password=PASSWORD)
        self.assertEqual(refused.exception.field, "code")
        self.assertNothingMade()

    def test_the_tenant_is_made_outside_the_accounts_transaction(self):
        """Copying and migrating a tenant takes seconds whenever the
        template is behind the code. Inside the accounts database's
        transaction - IMMEDIATE: SQLite's write lock from its first statement
        - every login and every session write of every bar waited for it."""
        seen = []

        def watching(step, real):
            def run(*args, **kwargs):
                seen.append((step, connections[ACCOUNTS_ALIAS].in_atomic_block))
                return real(*args, **kwargs)

            return run

        with (
            mock.patch.object(provisioning, "copy_database", watching("copie", provisioning.copy_database)),
            mock.patch.object(provisioning, "_migrate_bound", watching("migrate", provisioning._migrate_bound)),
        ):
            _user, tenant = signup.sign_up(
                code=self.code, bar_name="Bar Rapide", email="rapide@example.invalid", password=PASSWORD
            )
        self.assertEqual(seen, [("copie", False), ("migrate", False)])
        self.assertEqual(Membership.objects.get().tenant, tenant)
        self.assertTrue(paths.tenant_database(tenant).is_file())

    def test_a_code_or_an_address_taken_while_the_tenant_was_made_leaves_nothing(self):
        """The code and the address are checked again with the rows, in the
        short transaction: taken by another signup while this one's tenant
        was being copied, the signup is refused and that tenant removed."""
        real_copy = provisioning.copy_database

        def code_taken(*args, **kwargs):
            real_copy(*args, **kwargs)
            Invitation.objects.filter(pk=self.invitation.pk).update(used_at=timezone.now())

        def address_taken(*args, **kwargs):
            real_copy(*args, **kwargs)
            get_user_model().objects.create_user(username="plus-rapide", email="lente@example.invalid", password="x")

        for taken, meanwhile in (("code", code_taken), ("email", address_taken)):
            with self.subTest(taken=taken):
                try:
                    with mock.patch.object(provisioning, "copy_database", meanwhile):
                        with self.assertRaises(signup.SignupRefused) as refused:
                            signup.sign_up(
                                code=self.code, bar_name="Bar Lent", email="lente@example.invalid", password=PASSWORD
                            )
                    # Both said on the code's field, in the same words (ANON-5).
                    self.assertEqual(
                        (refused.exception.field, refused.exception.message), ("code", signup.CODE_REFUSED)
                    )
                    self.assertFalse(Tenant.objects.exists())
                    self.assertFalse(Membership.objects.exists())
                    self.assertEqual([p.name for p in paths.tenants_root().iterdir()], [paths.TEMPLATE_DIR])
                    self.assertFalse(get_user_model().objects.filter(username="lente@example.invalid").exists())
                finally:
                    # The other signup's traces: the next case starts clean.
                    Membership.objects.all().delete()
                    Tenant.objects.all().delete()
                    get_user_model().objects.all().delete()
                    Invitation.objects.filter(pk=self.invitation.pk).update(used_at=None, used_by=None)

    def test_a_refused_code_makes_no_tenant_at_all(self):
        """Refused before anything is copied: a wrong code costs a query,
        never a tenant made and thrown away."""
        with mock.patch.object(provisioning, "copy_database") as copy:
            with self.assertRaises(signup.SignupRefused):
                signup.sign_up(
                    code="ABCD-EFGH-JKLM-NPQR-STUV-WXYZ",
                    bar_name="Bar Faux",
                    email="faux@example.invalid",
                    password=PASSWORD,
                )
        copy.assert_not_called()
        self.assertNothingMade()

    def test_one_code_opens_one_signup(self):
        self.assertRedirects(self.post(), "/", fetch_redirect_response=False)
        other = Client()
        response = self.post(client=other, email="autre@example.invalid", bar_name="Bar Deux")
        self.assertContains(response, "ouvre pas d&#x27;inscription")
        self.assertEqual(Tenant.objects.count(), 1)


class SignupLimiterTests(SignupTestCase):
    def test_past_ten_refused_codes_even_a_good_one_waits(self):
        for n in range(limiter.LIMIT):
            self.post(code=f"FAUX-CODE-{n:04d}", email=f"essai{n}@example.invalid")
        with mock.patch.object(invitations, "usable_invitation") as checked:
            response = self.post()
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "réessayez dans un quart d&#x27;heure", status_code=429)
        checked.assert_not_called()
        self.assertNothingMade()
        self.assertUnused()
        # From elsewhere, with another address, the good code still works.
        response = self.post(ip="198.51.100.30", email="ailleurs@example.invalid")
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_an_address_is_counted_wherever_it_comes_from(self):
        for n in range(limiter.LIMIT):
            self.post(code=f"FAUX-CODE-{n:04d}", ip=f"192.0.2.{n + 1}")
        self.assertEqual(self.post(ip="198.51.100.31").status_code, 429)
        self.assertUnused()

    def test_an_ipv6_64_is_one_place(self):
        """Invitation codes guessed from ten addresses of one /64, each
        under another e-mail: one place, held at its tenth (review of 29/09,
        LIMITER-LOCKOUT - the place the login counts is the signup's)."""
        for n in range(limiter.LIMIT):
            self.post(code=f"FAUX-CODE-{n:04d}", email=f"essai{n}@example.invalid", ip=f"2001:db8:5:5::{n + 1:x}")
        with mock.patch.object(invitations, "usable_invitation") as checked:
            response = self.post(ip="2001:db8:5:5:ffff::1", email="encore@example.invalid")
        self.assertEqual(response.status_code, 429)
        checked.assert_not_called()
        self.assertUnused()


class SignupRemembersTheDeviceTests(SignupTestCase):
    """The signup logs its new owner in: that browser is a device the
    address logged in on (accounts/limiter.py, « appareil connu »)."""

    def test_the_new_owner_s_browser_is_a_known_device(self):
        response = self.post()
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        cookie = response.cookies[limiter.DEVICE_COOKIE]
        self.assertTrue(cookie["httponly"])
        request = mock.Mock(COOKIES={limiter.DEVICE_COOKIE: cookie.value})
        self.assertTrue(limiter.known_device(request, "gerante@example.invalid"))
        self.assertFalse(limiter.known_device(request, "autre@example.invalid"))

    def test_a_refused_signup_remembers_nothing(self):
        response = self.post(code="FAUX-CODE-0000")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(limiter.DEVICE_COOKIE, response.cookies)
