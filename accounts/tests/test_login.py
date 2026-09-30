"""« Connexion » and « Se déconnecter » (accounts/pages.py), in multi mode
for real: the login by e-mail, where it sends back to (this site only), the
failure limiter (accounts/limiter.py), CSRF, and the logout - for a user
with no espace too. Addresses invented (the documentation ranges: 192.0.2.x,
198.51.100.x, 203.0.113.x and 2001:db8::/32)."""

import base64
import json
import math
import threading
import time
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.cache.backends import locmem
from django.core.handlers.wsgi import WSGIRequest
from django.db import connections
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from accounts import limiter
from accounts.tests.support import TwoTenantsTestCase
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

LOGIN = reverse("accounts:login")
LOGOUT = reverse("accounts:logout")
ADMIN_LOGIN = reverse("admin:login")
PASSWORD = "mot-de-passe-essai"
#: A real hasher (the suite's is MD5, for speed): a check that takes time.
PBKDF2 = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
#: The PC itself, as `manage.py serve` sees a browser opened on it.
THIS_PC = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8765"}


class LimiterCacheMixin:
    """Every test starts with no failure counted (the cache is the
    process's, shared by the tests of this process)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class LoginTests(LimiterCacheMixin, TwoTenantsTestCase):
    def log_in(self, email="alpha@example.invalid", password=PASSWORD, client=None, **extra):
        return (client or self.client).post(LOGIN, {"username": email, "password": password, **extra}, **self.ip())

    def ip(self, address="192.0.2.10"):
        return {"REMOTE_ADDR": address}

    def test_the_page_is_french_and_counts_nothing(self):
        """Nothing bound, nothing counted: the navigation's badges are not on
        it, in nobody's database, and not a query is made."""
        with self.assertNumQueries(0, using="default"), self.assertNumQueries(0, using="accounts"):
            response = self.client.get(LOGIN)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        for words in ("Connexion", "Adresse e-mail", "Mot de passe", "Se connecter", "créer votre espace"):
            self.assertIn(words, page)
        self.assertNotIn("<nav", page)
        self.assertNotIn("badge", page)
        assertNoUnrenderedTemplateSyntax(self, response, LOGIN)

    def test_a_login_by_e_mail_opens_his_espace(self):
        response = self.log_in("  Alpha@Example.INVALID ")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        page = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Bar Alpha")

    def test_a_wrong_password_is_said_in_french_and_logs_nobody_in(self):
        response = self.log_in(password="pas-le-bon")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Adresse e-mail ou mot de passe incorrect.")
        self.assertNotIn("_auth_user_id", self.client.session)
        # An address nobody has reads exactly the same.
        other = self.log_in("personne@example.invalid")
        self.assertContains(other, "Adresse e-mail ou mot de passe incorrect.")

    def test_an_inactive_login_is_refused(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_active=False)
        response = self.log_in()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_login_whose_username_is_not_its_address(self):
        """`createsuperuser --database accounts` asks a username AND an
        address: the address typed on the page still finds the login."""
        owner = get_user_model().objects.create_user(
            username="proprio", email="Proprio@Example.invalid", password=PASSWORD
        )
        from accounts.models import Membership

        Membership.objects.create(user=owner, tenant=self.bar_a)
        response = self.log_in("proprio@example.invalid")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), owner.pk)

    def test_an_address_two_logins_share_names_neither(self):
        User = get_user_model()
        User.objects.create_user(username="un", email="double@example.invalid", password=PASSWORD)
        User.objects.create_user(username="deux", email="double@example.invalid", password=PASSWORD)
        response = self.log_in("double@example.invalid")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_next_on_this_site_is_where_the_login_goes(self):
        target = reverse("invoices:invoice_list") + "?onglet=achats"
        page = self.client.get(reverse("invoices:invoice_list"), {"onglet": "achats"})
        self.assertEqual(page["Location"], f"{LOGIN}?next=/invoices/%3Fonglet%3Dachats")
        form = self.client.get(page["Location"])
        self.assertContains(form, 'name="next" value="/invoices/?onglet=achats"')
        response = self.log_in(next=target)
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_next_never_leaves_this_site(self):
        for target in (
            "https://ailleurs.example/piege/",
            "//ailleurs.example/piege/",
            "/\\ailleurs.example/piege/",
            "http:ailleurs.example",
            "javascript:alert(1)",
            "https://testserver.ailleurs.example/",
        ):
            with self.subTest(target=target):
                self.client.logout()
                form = self.client.get(LOGIN, {"next": target})
                self.assertNotContains(form, "ailleurs")
                response = self.log_in(next=target)
                self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_logged_in_the_page_sends_home_and_never_away(self):
        self.client.force_login(self.user_a)
        self.assertRedirects(self.client.get(LOGIN), "/", fetch_redirect_response=False)
        response = self.client.get(LOGIN, {"next": "https://ailleurs.example/"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_a_next_naming_the_login_page_itself_goes_home(self):
        """Django's LoginView raises « Redirection loop » (a 500) for a
        logged-in visit whose next is the login page: a link anyone can
        build."""
        self.client.force_login(self.user_a)
        for target in (LOGIN, LOGIN + "?next=/"):
            with self.subTest(target=target):
                response = self.client.get(LOGIN, {"next": target})
                self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.client.logout()
        response = self.log_in(next=LOGIN)
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        refused = client.post(LOGIN, {"username": "alpha@example.invalid", "password": PASSWORD})
        self.assertEqual(refused.status_code, 403)
        client.get(LOGIN)
        token = client.cookies["csrftoken"].value
        response = client.post(
            LOGIN, {"username": "alpha@example.invalid", "password": PASSWORD, "csrfmiddlewaretoken": token}
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)


class LoginLimiterTests(LimiterCacheMixin, TwoTenantsTestCase):
    """The hard stop is one address from one place; one address from
    everywhere and one place for every address have much higher ceilings
    (accounts/limiter.py; security audit ANON-4, DEPLOY-4)."""

    def attempt(self, email, password="pas-le-bon", ip="192.0.2.10"):
        return self.client.post(LOGIN, {"username": email, "password": password}, REMOTE_ADDR=ip)

    def fail_times(self, times, email="alpha@example.invalid", ip="192.0.2.10"):
        # Not `fail`: that is unittest's own, which every failing assertion calls.
        for _ in range(times):
            self.assertEqual(self.attempt(email, ip=ip).status_code, 200)

    def test_the_ceilings_are_much_higher_than_the_hard_stop(self):
        self.assertEqual(limiter.LIMIT, 10)
        self.assertGreaterEqual(limiter.EMAIL_LIMIT, 5 * limiter.LIMIT)
        self.assertGreaterEqual(limiter.IP_LIMIT, 5 * limiter.LIMIT)

    def test_a_stranger_s_ten_failures_hold_his_places_not_the_owner_s(self):
        """ANON-4: ten wrong passwords for the owner's address, sent by a
        stranger from his own place - or from ten - held the owner's RIGHT
        password back everywhere for a quarter of an hour. Ten failures now
        hold the place they came from. (What a stranger CAN still fill is
        the address's ceiling, from many places: a device the owner logged
        in on, and the PC itself, are never held by it - KnownDeviceTests,
        ThisPcTests.)"""
        for n in range(limiter.LIMIT):
            self.fail_times(1, ip=f"203.0.113.{n + 1}")
        self.fail_times(limiter.LIMIT, ip="203.0.113.99")
        response = self.attempt("alpha@example.invalid", PASSWORD, ip="198.51.100.7")
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_past_ten_failures_from_one_place_that_place_waits_even_with_the_right_password(self):
        self.fail_times(limiter.LIMIT, ip="203.0.113.5")
        with mock.patch("django.contrib.auth.forms.authenticate") as checked:
            response = self.attempt("alpha@example.invalid", PASSWORD, ip="203.0.113.5")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "réessayez dans un quart d&#x27;heure", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)
        checked.assert_not_called()
        # The same address from its owner's place is let in.
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD, ip="198.51.100.7").status_code, 302)

    def test_past_its_ceiling_an_address_waits_from_every_unknown_place(self):
        """Guesses spread over many places stop at the address's ceiling -
        for every device the address never logged in on, and every place
        but the PC itself (KnownDeviceTests, ThisPcTests)."""
        with mock.patch.object(limiter, "EMAIL_LIMIT", 12):
            for n in range(12):
                self.fail_times(1, ip=f"192.0.2.{n + 1}")
            with mock.patch("django.contrib.auth.forms.authenticate") as checked:
                response = self.attempt("alpha@example.invalid", PASSWORD, ip="198.51.100.7")
            self.assertEqual(response.status_code, 429)
            checked.assert_not_called()
            # Another address from that place is not held back.
            self.assertEqual(self.attempt("beta@example.invalid", PASSWORD, ip="198.51.100.7").status_code, 302)

    def test_past_its_ceiling_one_place_waits_for_every_address(self):
        """One place trying a few passwords on many addresses stops at the
        place's ceiling."""
        with mock.patch.object(limiter, "IP_LIMIT", 12):
            for n in range(12):
                self.fail_times(1, email=f"essai{n}@example.invalid")
            response = self.attempt("beta@example.invalid", PASSWORD)
            self.assertEqual(response.status_code, 429)
            self.assertNotIn("_auth_user_id", self.client.session)
            # The same address from another place is let in.
            self.assertEqual(self.attempt("beta@example.invalid", PASSWORD, ip="198.51.100.9").status_code, 302)

    def test_nine_failures_are_not_ten(self):
        self.fail_times(limiter.LIMIT - 1)
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD).status_code, 302)

    def test_a_success_forgets_the_address_not_the_place(self):
        # The place's ceiling brought down to the hard stop, to see it held.
        self.enterContext(mock.patch.object(limiter, "IP_LIMIT", limiter.LIMIT))
        self.fail_times(limiter.LIMIT - 1)
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD).status_code, 302)
        self.client.logout()
        # The address starts again from nothing...
        self.fail_times(limiter.LIMIT - 1, ip="198.51.100.3")
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD, ip="198.51.100.3").status_code, 302)
        self.client.logout()
        # ...the place does not: its tenth failure holds it.
        self.fail_times(1, email="autre@example.invalid")
        self.assertEqual(self.attempt("beta@example.invalid", PASSWORD).status_code, 429)

    def test_the_window_ends(self):
        self.fail_times(limiter.LIMIT)
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD).status_code, 429)
        later = locmem.time.time() + limiter.WINDOW_SECONDS + 1

        class Later:
            @staticmethod
            def time():
                return later

        with mock.patch.object(locmem, "time", Later):
            self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD).status_code, 302)

    def test_a_malformed_post_is_counted_too(self):
        # No password: counted for the address it names.
        for _ in range(limiter.LIMIT):
            self.client.post(LOGIN, {"username": "alpha@example.invalid"}, REMOTE_ADDR="192.0.2.10")
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD).status_code, 429)
        # No address at all: counted for the place.
        with mock.patch.object(limiter, "IP_LIMIT", limiter.LIMIT):
            for _ in range(limiter.LIMIT):
                self.client.post(LOGIN, {"username": "pas-une-adresse"}, REMOTE_ADDR="192.0.2.11")
            self.assertEqual(self.attempt("beta@example.invalid", PASSWORD, ip="192.0.2.11").status_code, 429)

    def test_guesses_sent_at_once_are_held_to_the_limit(self):
        """Checked first and counted once the password was wrong, guesses
        arriving together all passed the check before the first of them was
        counted - as many guesses as one can send at once (a threaded
        server, several processes). Each is counted BEFORE its password is
        looked at: never more than LIMIT checked, whatever arrives at once.
        A real hasher, so that the checks overlap."""
        parallel = 25
        checked, statuses = [], []
        lock = threading.Lock()
        start = threading.Barrier(parallel)

        def counting(*args, **kwargs):
            with lock:
                checked.append(1)
            return authenticate(*args, **kwargs)

        def guess(n):
            try:
                client = Client()
                start.wait()
                response = client.post(
                    LOGIN, {"username": "alpha@example.invalid", "password": f"faux-{n}"}, REMOTE_ADDR="192.0.2.60"
                )
                with lock:
                    statuses.append(response.status_code)
            finally:
                connections.close_all()

        with override_settings(PASSWORD_HASHERS=PBKDF2):
            self.user_a.set_password(PASSWORD)
            self.user_a.save()
            with mock.patch("django.contrib.auth.forms.authenticate", counting):
                threads = [threading.Thread(target=guess, args=(n,)) for n in range(parallel)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(len(statuses), parallel)
        self.assertLessEqual(len(checked), limiter.LIMIT)
        self.assertEqual(statuses.count(200), len(checked))
        self.assertEqual(statuses.count(429), parallel - len(checked))

    def test_a_refused_attempt_counts_nothing(self):
        """Past the limit nothing is checked - and nothing is counted: guesses
        from a place held back never add to the count of the address they
        name (it would lock its owner out from everywhere). Both ceilings
        brought down to the hard stop, to see it."""
        self.enterContext(mock.patch.object(limiter, "IP_LIMIT", limiter.LIMIT))
        self.enterContext(mock.patch.object(limiter, "EMAIL_LIMIT", limiter.LIMIT))
        self.fail_times(limiter.LIMIT, email="autre@example.invalid")
        for _ in range(limiter.LIMIT):
            self.assertEqual(self.attempt("alpha@example.invalid").status_code, 429)
        self.assertEqual(self.attempt("alpha@example.invalid", PASSWORD, ip="198.51.100.8").status_code, 302)


ALPHA = "alpha@example.invalid"
BETA = "beta@example.invalid"
#: The PC's own names beside the public one, as the owner's .env lists them.
HOSTS = ["testserver", "127.0.0.1", "localhost", "[::1]", "gestion.example.com"]


class CeilingMixin(LimiterCacheMixin):
    """A stranger filling an address's ceiling (`EMAIL_LIMIT`) from many
    places, each to its hard stop: no place is held back, and the address
    is held from every place it does not know."""

    def post_login(self, client, email, password="pas-le-bon", **meta):
        meta.setdefault("REMOTE_ADDR", "192.0.2.10")
        return client.post(LOGIN, {"username": email, "password": password}, **meta)

    def fill_the_ceiling(self, email=ALPHA):
        stranger = Client()
        for place in range(math.ceil(limiter.EMAIL_LIMIT / limiter.LIMIT)):
            for _ in range(limiter.LIMIT):
                response = self.post_login(stranger, email, REMOTE_ADDR=f"203.0.113.{place + 1}")
                self.assertEqual(response.status_code, 200)
        # Held now for a device it never logged in on, from a place that
        # never failed - the right password not even looked at.
        with mock.patch("django.contrib.auth.forms.authenticate") as checked:
            response = self.post_login(Client(), email, PASSWORD, REMOTE_ADDR="198.51.100.200")
        self.assertEqual(response.status_code, 429)
        checked.assert_not_called()

    def owner_s_device(self, email=ALPHA):
        """A browser the owner logged in on, then left: the « appareil
        connu » cookie outlives the session."""
        device = Client()
        response = self.post_login(device, email, PASSWORD, REMOTE_ADDR="198.51.100.7")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        logout = device.post(LOGOUT)
        self.assertNotIn(limiter.DEVICE_COOKIE, logout.cookies)
        self.assertTrue(device.cookies[limiter.DEVICE_COOKIE].value)
        return device


@override_settings(ALLOWED_HOSTS=HOSTS)
class KnownDeviceTests(CeilingMixin, TwoTenantsTestCase):
    """A stranger who knows the owner's address could fill its ceiling from
    many places and keep the owner out of both doors, the PC included, for
    as long as he went on sending (review of 29/09, LIMITER-LOCKOUT: ten
    addresses of one IPv6 /64, ten failures each). Every successful login
    now leaves a signed « appareil connu » cookie for that address, and an
    attempt carrying one is judged on its place and on that device's own
    failures - never on the address's ceiling."""

    def test_a_login_remembers_its_device(self):
        response = self.post_login(self.client, ALPHA, PASSWORD)
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        cookie = response.cookies[limiter.DEVICE_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(int(cookie["max-age"]), 180 * 24 * 3600)
        # The test settings are plain http (MARGINMATE_HTTPS off).
        self.assertFalse(cookie["secure"])
        # The address is not in it, written or decoded: a shared device
        # does not tell who logged in on it.
        payload = signing.loads(cookie.value, salt=limiter.DEVICE_SALT)
        self.assertNotIn("alpha", json.dumps(payload).lower())
        written = cookie.value.split(":")[0]
        self.assertNotIn("alpha", base64.urlsafe_b64decode(written + "=" * (-len(written) % 4)).decode().lower())

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_over_https_it_travels_over_https_only(self):
        """MARGINMATE_HTTPS=1 makes the session cookie Secure; the device's
        goes with it."""
        response = self.post_login(self.client, ALPHA, PASSWORD)
        self.assertTrue(response.cookies[limiter.DEVICE_COOKIE]["secure"])

    def test_a_wrong_password_remembers_nothing(self):
        response = self.post_login(self.client, ALPHA)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(limiter.DEVICE_COOKIE, response.cookies)

    def test_a_known_device_is_never_held_by_the_address_ceiling(self):
        """The finding: the address's ceiling filled by a stranger, the
        owner's RIGHT password still opens from the device he logged in on,
        wherever it is - and the ceiling still holds every other device."""
        device = self.owner_s_device()
        self.fill_the_ceiling()
        with mock.patch("django.contrib.auth.forms.authenticate", wraps=authenticate) as checked:
            response = self.post_login(device, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        checked.assert_called_once()
        self.assertEqual(self.post_login(Client(), ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.9").status_code, 429)

    def test_the_admin_door_knows_the_device_too(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        device = self.owner_s_device()
        self.fill_the_ceiling()
        form = {"username": ALPHA, "password": PASSWORD, "next": "/admin/"}
        response = device.post(ADMIN_LOGIN, form, REMOTE_ADDR="198.51.100.8")
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        response = Client().post(ADMIN_LOGIN, form, REMOTE_ADDR="198.51.100.9")
        self.assertContains(response, "réessayez dans un quart d&#x27;heure")

    def test_a_device_is_known_for_the_addresses_logged_in_on_it_only(self):
        device = self.owner_s_device(ALPHA)
        self.fill_the_ceiling(BETA)
        self.assertEqual(self.post_login(device, BETA, PASSWORD, REMOTE_ADDR="198.51.100.8").status_code, 429)

    def test_a_shared_device_knows_each_address_logged_in_on_it(self):
        device = self.owner_s_device(ALPHA)
        self.assertEqual(self.post_login(device, BETA, PASSWORD, REMOTE_ADDR="198.51.100.7").status_code, 302)
        device.post(LOGOUT)
        self.fill_the_ceiling(ALPHA)
        self.fill_the_ceiling(BETA)
        for email in (ALPHA, BETA):
            with self.subTest(email=email):
                response = self.post_login(device, email, PASSWORD, REMOTE_ADDR="198.51.100.8")
                self.assertRedirects(response, "/", fetch_redirect_response=False)
                device.post(LOGOUT)

    def test_a_device_keeps_a_few_addresses_the_last_first(self):
        with mock.patch.object(limiter, "DEVICE_ADDRESSES", 1):
            device = self.owner_s_device(ALPHA)
            self.assertEqual(self.post_login(device, BETA, PASSWORD, REMOTE_ADDR="198.51.100.7").status_code, 302)
            device.post(LOGOUT)
        self.fill_the_ceiling(ALPHA)
        self.fill_the_ceiling(BETA)
        self.assertEqual(self.post_login(device, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8").status_code, 429)
        self.assertEqual(self.post_login(device, BETA, PASSWORD, REMOTE_ADDR="198.51.100.8").status_code, 302)

    def test_a_forged_foreign_or_old_cookie_is_no_known_device(self):
        self.fill_the_ceiling()
        fresh = limiter.device_cookie([ALPHA])
        with mock.patch("django.core.signing.time.time", return_value=time.time() - limiter.DEVICE_MAX_AGE - 60):
            old = limiter.device_cookie([ALPHA])
        foreign = signing.dumps(signing.loads(fresh, salt=limiter.DEVICE_SALT), salt="une.autre.application")
        for name, value in {
            "garbage": "n'importe quoi",
            "tampered": fresh[:-3] + ("aaa" if not fresh.endswith("aaa") else "bbb"),
            "older than 180 days": old,
            "signed for something else": foreign,
            "another address's": limiter.device_cookie([BETA]),
        }.items():
            with self.subTest(name):
                client = Client()
                client.cookies[limiter.DEVICE_COOKIE] = value
                response = self.post_login(client, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8")
                self.assertEqual(response.status_code, 429)
        # The fresh one opens: the shapes above are refused for what they are.
        client = Client()
        client.cookies[limiter.DEVICE_COOKIE] = fresh
        self.assertEqual(self.post_login(client, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8").status_code, 302)

    def test_a_known_device_still_meets_its_place_s_hard_stop(self):
        device = self.owner_s_device()
        for _ in range(limiter.LIMIT):
            self.assertEqual(self.post_login(device, ALPHA, REMOTE_ADDR="198.51.100.8").status_code, 200)
        self.assertEqual(self.post_login(device, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8").status_code, 429)

    def test_a_known_device_s_own_failures_count_wherever_it_goes(self):
        """A cookie copied off the owner's device is no pass for guesses
        spread over many places: the device has its own hard stop - which
        no stranger can move, not holding the cookie - and its failures
        are none of the address's ceiling."""
        device = self.owner_s_device()
        for n in range(limiter.LIMIT):
            self.assertEqual(self.post_login(device, ALPHA, REMOTE_ADDR=f"192.0.2.{n + 100}").status_code, 200)
        self.assertEqual(self.post_login(device, ALPHA, PASSWORD, REMOTE_ADDR="192.0.2.200").status_code, 429)
        self.assertEqual(self.post_login(Client(), ALPHA, PASSWORD, REMOTE_ADDR="192.0.2.201").status_code, 302)

    def test_a_logout_keeps_the_device_and_a_success_renews_it(self):
        device = self.owner_s_device()
        first = device.cookies[limiter.DEVICE_COOKIE].value
        response = self.post_login(device, ALPHA, PASSWORD, REMOTE_ADDR="198.51.100.8")
        renewed = response.cookies[limiter.DEVICE_COOKIE].value
        # The same device (its id), its 180 days counted again.
        self.assertEqual(
            signing.loads(renewed, salt=limiter.DEVICE_SALT)["n"], signing.loads(first, salt=limiter.DEVICE_SALT)["n"]
        )


class PlaceTests(SimpleTestCase):
    """What the counters call a place (`limiter.client_place`)."""

    def place(self, address):
        return limiter.client_place(RequestFactory().get("/", REMOTE_ADDR=address))

    def test_an_ipv4_address_is_its_own_place(self):
        self.assertEqual(self.place("203.0.113.7"), "203.0.113.7")

    def test_an_ipv6_address_is_its_64(self):
        for address in ("2001:db8:1:1::5", "2001:DB8:1:1:ffff:ffff:ffff:ffff", "2001:0db8:0001:0001:0:0:0:1"):
            with self.subTest(address=address):
                self.assertEqual(self.place(address), "2001:db8:1:1::/64")
        self.assertEqual(self.place("fe80::1%12"), "fe80::/64")

    def test_an_ipv4_client_written_as_ipv6_is_its_ipv4_address(self):
        """A server listening on [::] writes an IPv4 client ::ffff:a.b.c.d,
        whose /64 is ::/64 - every IPv4 client in the world at once."""
        self.assertEqual(self.place("::ffff:203.0.113.7"), "203.0.113.7")

    def test_what_is_no_address_stays_as_it_is(self):
        for address in ("", "pas-une-adresse", "unix:/tmp/socket"):
            with self.subTest(address=address):
                self.assertEqual(self.place(address), address)


class Ipv6PlacesTests(LimiterCacheMixin, TwoTenantsTestCase):
    """An IPv6 client is its /64, for the hard stop and for the place's
    ceiling: Cloudflare hands over the visitor's full address, and one /64
    - a phone's network, a rented server's allocation - is 2^64 addresses
    free to rotate in (review of 29/09, LIMITER-LOCKOUT)."""

    def post(self, email, password="pas-le-bon", ip="2001:db8:1:1::1"):
        return self.client.post(LOGIN, {"username": email, "password": password}, REMOTE_ADDR=ip)

    def test_one_64_is_one_place(self):
        """The finding: ten addresses of ONE /64 were ten places, each with
        its own ten failures - a hundred guesses, the address's ceiling."""
        for n in range(limiter.LIMIT):
            self.assertEqual(self.post(ALPHA, ip=f"2001:db8:1:1::{n + 1:x}").status_code, 200)
        with mock.patch("django.contrib.auth.forms.authenticate") as checked:
            response = self.post(ALPHA, PASSWORD, ip="2001:db8:1:1:ffff::1")
        self.assertEqual(response.status_code, 429)
        checked.assert_not_called()
        # The next /64 is another place.
        self.assertEqual(self.post(ALPHA, PASSWORD, ip="2001:db8:1:2::1").status_code, 302)

    def test_the_place_s_ceiling_counts_a_64_as_one_place(self):
        with mock.patch.object(limiter, "IP_LIMIT", 12):
            for n in range(12):
                self.assertEqual(self.post(f"essai{n}@example.invalid", ip=f"2001:db8:1:1:{n + 1:x}::1").status_code, 200)
            self.assertEqual(self.post(BETA, PASSWORD, ip="2001:db8:1:1:ffff::1").status_code, 429)
            self.assertEqual(self.post(BETA, PASSWORD, ip="2001:db8:1:2::1").status_code, 302)

    def test_an_ipv4_client_written_as_ipv6_is_counted_as_itself(self):
        # Ten IPv4 clients written ::ffff:… are ten places, not one ::/64...
        for n in range(limiter.LIMIT):
            self.assertEqual(self.post(ALPHA, ip=f"::ffff:203.0.113.{n + 1}").status_code, 200)
        self.assertEqual(self.post(ALPHA, PASSWORD, ip="::ffff:203.0.113.99").status_code, 302)
        self.client.logout()
        # ...and one written both ways is one place.
        for _ in range(limiter.LIMIT - 1):
            self.post(BETA, ip="203.0.113.50")
        self.post(BETA, ip="::ffff:203.0.113.50")
        self.assertEqual(self.post(BETA, PASSWORD, ip="203.0.113.50").status_code, 429)


@override_settings(ALLOWED_HOSTS=HOSTS)
class ThisPcTests(CeilingMixin, TwoTenantsTestCase):
    """The owner at the PC running `serve`: a browser there reaches Waitress
    directly, as 127.0.0.1. The address's ceiling never holds him there,
    with no cookie either (a new browser, one that forgot). A request that
    came through the tunnel is never the PC: Waitress makes cloudflared's
    requests the visitor's, and one carrying a proxy's mark, or naming a
    host that is not the PC, is not the PC whatever its address."""

    def test_the_pc_itself_is_never_held_by_the_address_ceiling(self):
        self.fill_the_ceiling()
        for meta in (
            THIS_PC,
            {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "localhost:8765"},
            {"REMOTE_ADDR": "::1", "HTTP_HOST": "[::1]:8765"},
        ):
            with self.subTest(meta=meta):
                response = self.post_login(Client(), ALPHA, PASSWORD, **meta)
                self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_the_pc_still_meets_its_hard_stop(self):
        for _ in range(limiter.LIMIT):
            self.assertEqual(self.post_login(self.client, ALPHA, **THIS_PC).status_code, 200)
        self.assertEqual(self.post_login(self.client, ALPHA, PASSWORD, **THIS_PC).status_code, 429)

    def test_a_request_bearing_a_proxy_s_mark_is_never_the_pc(self):
        """From 127.0.0.1 with a proxy's mark it came through one: runserver
        answering the tunnel (it reads no X-Forwarded-For: every visitor is
        127.0.0.1), or cloudflared with a header Waitress did not act on."""
        self.fill_the_ceiling()
        marks = {
            "HTTP_X_FORWARDED_FOR": "203.0.113.9",
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_HOST": "gestion.example.com",
            "HTTP_FORWARDED": "for=203.0.113.9",
            "HTTP_X_REAL_IP": "203.0.113.9",
            "HTTP_TRUE_CLIENT_IP": "203.0.113.9",
            "HTTP_CF_CONNECTING_IP": "203.0.113.9",
            "HTTP_CF_RAY": "8c0ffee000000000-CDG",
            "HTTP_CF_IPCOUNTRY": "FR",
            "HTTP_CDN_LOOP": "cloudflare",
        }
        for header, value in marks.items():
            with self.subTest(header=header):
                response = self.post_login(Client(), ALPHA, PASSWORD, **THIS_PC, **{header: value})
                self.assertEqual(response.status_code, 429)
        for host in ("gestion.example.com", "testserver", None):
            with self.subTest(host=host):
                meta = {"REMOTE_ADDR": "127.0.0.1"} | ({"HTTP_HOST": host} if host else {})
                self.assertEqual(self.post_login(Client(), ALPHA, PASSWORD, **meta).status_code, 429)


#: What cloudflared hands Waitress from 127.0.0.1: the visitor's address
#: (Cloudflare's edge appended it), Cloudflare's marks, the public host.
FROM_THE_TUNNEL = {
    "HTTP_X_FORWARDED_FOR": "203.0.113.9",
    "HTTP_X_FORWARDED_PROTO": "https",
    "HTTP_CF_CONNECTING_IP": "203.0.113.9",
    "HTTP_CF_RAY": "8c0ffee000000000-CDG",
    "HTTP_CDN_LOOP": "cloudflare",
    "HTTP_HOST": "gestion.example.com",
}


class ThroughWaitressTests(SimpleTestCase):
    """`limiter.directly_from_this_pc` on the environ Waitress hands over, set
    up as `serve` sets it (accounts/tests/test_serve.py's server, not
    started): cloudflared connects from 127.0.0.1 too, so what tells the
    two apart must hold whatever the tunnel sends."""

    def setUp(self):
        from accounts.tests.test_serve import close_server, tunnel_server

        self.seen = []

        def recording(env, start_response):
            self.seen.append(dict(env))
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        self.server = tunnel_server(recording)
        self.addCleanup(close_server, self.server)

    def is_the_pc(self, peer, **headers) -> bool:
        from accounts.tests.test_serve import call, environ

        self.seen.clear()
        call(self.server.application, environ(peer, **headers))
        (seen,) = self.seen
        return limiter.directly_from_this_pc(WSGIRequest(seen))

    def test_a_browser_on_the_pc_is_the_pc(self):
        self.assertTrue(self.is_the_pc("127.0.0.1", HTTP_HOST="127.0.0.1:8765"))
        self.assertTrue(self.is_the_pc("127.0.0.1", HTTP_HOST="localhost:8765"))

    def test_a_tunnelled_request_never_is(self):
        without_for = {name: value for name, value in FROM_THE_TUNNEL.items() if name != "HTTP_X_FORWARDED_FOR"}
        for name, headers in {
            "as the tunnel sends it": FROM_THE_TUNNEL,
            "naming the PC as its host": {**FROM_THE_TUNNEL, "HTTP_HOST": "127.0.0.1:8765"},
            "a visitor writing 127.0.0.1 first": {
                **FROM_THE_TUNNEL,
                "HTTP_X_FORWARDED_FOR": "127.0.0.1, 203.0.113.9",
                "HTTP_HOST": "127.0.0.1:8765",
            },
            "a forwarded address that is the PC's": {"HTTP_X_FORWARDED_FOR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8765"},
            "no X-Forwarded-For at all": {**without_for, "HTTP_HOST": "127.0.0.1:8765"},
            "Cloudflare's marks alone": {"HTTP_CF_RAY": "8c0ffee000000000-CDG", "HTTP_HOST": "127.0.0.1:8765"},
        }.items():
            with self.subTest(name):
                self.assertFalse(self.is_the_pc("127.0.0.1", **headers))

    def test_another_machine_never_is(self):
        self.assertFalse(self.is_the_pc("192.0.2.77", HTTP_HOST="127.0.0.1:8765"))
        self.assertFalse(self.is_the_pc("192.0.2.77", **{**FROM_THE_TUNNEL, "HTTP_X_FORWARDED_FOR": "127.0.0.1"}))


class AdminLoginLimiterTests(LimiterCacheMixin, TwoTenantsTestCase):
    """/admin/login/ is Django's own login page, public - a second door. In
    multi mode it counts on the login page's counters: the superuser, who
    reads every bar's logins, was guessed at there without any limit."""

    def setUp(self):
        super().setUp()
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)

    def admin_attempt(self, password, ip="192.0.2.50"):
        return self.client.post(
            ADMIN_LOGIN, {"username": "alpha@example.invalid", "password": password, "next": "/admin/"}, REMOTE_ADDR=ip
        )

    def test_the_admin_login_is_held_back_like_the_login_page(self):
        with mock.patch("django.contrib.auth.forms.authenticate", wraps=authenticate) as checked:
            for n in range(limiter.LIMIT * 3):
                self.assertEqual(self.admin_attempt(f"faux-{n}").status_code, 200)
            response = self.admin_attempt(PASSWORD)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertLessEqual(checked.call_count, limiter.LIMIT)
        self.assertContains(response, "réessayez dans un quart d&#x27;heure")
        # One count for both doors: the login page waits too.
        response = self.client.post(
            LOGIN, {"username": "alpha@example.invalid", "password": PASSWORD}, REMOTE_ADDR="192.0.2.50"
        )
        self.assertEqual(response.status_code, 429)

    def test_the_login_page_s_failures_hold_the_admin_back_too(self):
        for n in range(limiter.LIMIT):
            self.client.post(LOGIN, {"username": "alpha@example.invalid", "password": f"faux-{n}"}, REMOTE_ADDR="192.0.2.50")
        with mock.patch("django.contrib.auth.forms.authenticate", return_value=None) as checked:
            self.admin_attempt(PASSWORD)
        checked.assert_not_called()
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_right_password_still_opens_the_admin(self):
        self.admin_attempt("faux")
        response = self.admin_attempt(PASSWORD)
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user_a.pk)

    def test_its_login_leaves_the_known_device_cookie_as_the_login_page_does(self):
        """The admin's door honoured the cookie but never issued one: the
        superuser logging in at /admin/ alone stayed a stranger's device for
        the address's ceiling (review of LIMITER-LOCKOUT's fix)."""
        self.assertNotIn(limiter.DEVICE_COOKIE, self.admin_attempt("faux").cookies)
        response = self.admin_attempt(PASSWORD)
        self.assertEqual(response.status_code, 302)
        cookie = response.cookies[limiter.DEVICE_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        request = mock.Mock(COOKIES={limiter.DEVICE_COOKIE: cookie.value})
        self.assertTrue(limiter.known_device(request, "alpha@example.invalid"))
        # Already logged in, the admin's login page only redirects: no cookie.
        self.assertNotIn(limiter.DEVICE_COOKIE, self.client.get(ADMIN_LOGIN).cookies)

    def test_its_logout_is_django_s_own(self):
        """No override left: the one that asked the browser to empty its
        storage (Clear-Site-Data, gone with LOGOUT-PREFS) ended as a no-op."""
        from django.contrib import admin

        from accounts.admin_site import MarginMateAdminSite

        self.assertIs(MarginMateAdminSite.logout, admin.AdminSite.logout)
        from accounts import pages

        self.assertFalse(hasattr(pages, "forget_the_browser_storage"))

    def test_a_form_missing_its_password_forgets_nothing(self):
        """Nothing checked is no success: a POST with no password must not
        wipe the address's count between two guesses."""
        for n in range(limiter.LIMIT - 1):
            self.admin_attempt(f"faux-{n}", ip="198.51.100.40")
        # The tenth attempt on the address from that place, no password:
        # counted, not a success.
        self.client.post(ADMIN_LOGIN, {"username": "alpha@example.invalid"}, REMOTE_ADDR="198.51.100.40")
        with mock.patch("django.contrib.auth.forms.authenticate", return_value=None) as checked:
            self.admin_attempt(PASSWORD, ip="198.51.100.40")
        checked.assert_not_called()


class LogoutTests(LimiterCacheMixin, TwoTenantsTestCase):
    def test_logging_out_lands_on_the_login_page_saying_so(self):
        self.client.force_login(self.user_a)
        response = self.client.post(LOGOUT, follow=True)
        self.assertEqual(response.redirect_chain[0], (LOGIN, 302))
        self.assertContains(response, "Vous êtes déconnecté.")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("invoices:supplier_list")).status_code, 302)

    def test_it_is_a_post(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get(LOGOUT).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)

    def test_anonymous_it_only_goes_to_the_login_page(self):
        response = self.client.post(LOGOUT, follow=True)
        self.assertEqual(response.redirect_chain, [(LOGIN, 302)])
        self.assertNotContains(response, "Vous êtes déconnecté.")

    def test_its_next_never_leaves_this_site(self):
        self.client.force_login(self.user_a)
        response = self.client.post(LOGOUT + "?next=https://ailleurs.example/")
        self.assertRedirects(response, LOGIN, fetch_redirect_response=False)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user_a)
        self.assertEqual(client.post(LOGOUT).status_code, 403)
        self.assertIn("_auth_user_id", client.session)

    def test_a_login_with_no_espace_can_still_leave(self):
        nobody = self.make_member(self.bar_b, "sans-espace@example.invalid")
        nobody.memberships.all().delete()
        self.client.force_login(nobody)
        page = self.client.get(reverse("invoices:supplier_list"))
        self.assertEqual(page.status_code, 403)
        self.assertContains(page, f'action="{LOGOUT}"', status_code=403)
        self.assertContains(page, "Se déconnecter", status_code=403)
        assertNoUnrenderedTemplateSyntax(self, page, "aucun espace")
        # The login page sends him home, which is that page, not a loop.
        self.assertRedirects(self.client.get(LOGIN), "/", fetch_redirect_response=False)
        response = self.client.post(LOGOUT, follow=True)
        self.assertContains(response, "Vous êtes déconnecté.")
        self.assertNotIn("_auth_user_id", self.client.session)
