"""The devices' rules (notifications/devices.py): « Activer » alone creates
or moves one, the sync never creates, the cookie names a device for its
login only, a logout marks it and another login on the browser deletes
it. User agents, endpoints and keys are invented."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.db import DatabaseError
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, PushDevice, Tenant
from notifications import devices, sending, webpush
from notifications.tests.support import SITE, QuietLogs, make_device, make_login, new_endpoint, new_keys
from tests.runner import TEST_TENANT, TEST_TENANT_PK, test_user

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.4 Mobile/15E148 Safari/604.1"
)
ANDROID = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Mobile Safari/537.36"
)
WINDOWS_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)
MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"
)
LINUX_FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAMSUNG = (
    "Mozilla/5.0 (Linux; Android 15; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)


def cookie_value(device: PushDevice) -> str:
    """The value `set_cookie` writes for `device`, as the browser keeps it."""
    return devices.set_cookie(HttpResponse(), device).cookies[devices.COOKIE_NAME].value


def request_with(cookie: str | None = None, *, user=None, host="testserver"):
    request = RequestFactory().get("/", HTTP_HOST=host)
    if cookie is not None:
        request.COOKIES[devices.COOKIE_NAME] = cookie
    if user is not None:
        request.user = user
    return request


class LabelTests(TestCase):
    def test_what_the_user_agent_says(self):
        for agent, label in (
            (IPHONE, "iPhone · application"),
            (ANDROID, "Android · Chrome"),
            (WINDOWS_EDGE, "Windows · Edge"),
            (MAC_SAFARI, "Mac · Safari"),
            (LINUX_FIREFOX, "Linux · Firefox"),
            (SAMSUNG, "Android · Samsung Internet"),
            ("", "Appareil"),
            (None, "Appareil"),
            ("curl/8.0", "Appareil"),
        ):
            with self.subTest(label=label):
                self.assertEqual(devices.label_from_user_agent(agent), label)


class Logins(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.member = make_login(TEST_TENANT_PK, "serveur@example.invalid")
        self.other = make_login(TEST_TENANT_PK, "barman@example.invalid")
        self.key = webpush.vapid_public_key()

    def subscription(self, endpoint=None, keys=None, server_key=None) -> dict:
        p256dh, auth = keys or new_keys()
        return {
            "endpoint": endpoint or new_endpoint(),
            "p256dh": p256dh,
            "auth": auth,
            "server_key": self.key if server_key is None else server_key,
        }


class CookieTests(Logins):
    def test_it_names_a_device_and_its_login_signed(self):
        device = make_device(self.member)
        response = devices.set_cookie(HttpResponse(), device)
        morsel = response.cookies[devices.COOKIE_NAME]
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["samesite"], "Lax")
        self.assertEqual(morsel["max-age"], 400 * 24 * 3600)
        self.assertIn(f"{device.pk}:{self.member.user_id}", morsel.value)
        self.assertEqual(devices.read_cookie(request_with(morsel.value)), (device.pk, self.member.user_id))

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_secure_like_the_session_cookie(self):
        response = devices.set_cookie(HttpResponse(), make_device(self.member))
        self.assertTrue(response.cookies[devices.COOKIE_NAME]["secure"])

    def test_anything_else_names_nothing(self):
        value = cookie_value(make_device(self.member))
        forged = value.replace(f"{self.member.user_id}:", f"{self.other.user_id}:", 1)
        for cookie in (None, "", "12:3", forged, value + "x", "a:b:c"):
            with self.subTest(cookie=cookie):
                self.assertIsNone(devices.read_cookie(request_with(cookie)))

    def test_cleared(self):
        response = devices.clear_cookie(HttpResponse())
        self.assertEqual(response.cookies[devices.COOKIE_NAME]["max-age"], 0)


class RegisterTests(Logins):
    def test_activer_creates_the_device(self):
        values = self.subscription()
        device = devices.register(self.member, user_agent=ANDROID, **values)
        self.assertEqual(device.membership, self.member)
        self.assertEqual(
            (device.endpoint, device.server_key, device.label), (values["endpoint"], self.key, "Android · Chrome")
        )

    def test_the_same_browser_moves_with_its_keys_only(self):
        values = self.subscription()
        device = devices.register(self.member, **values)
        moved = devices.register(self.other, user_agent=IPHONE, **values)
        self.assertEqual(moved.pk, device.pk)
        self.assertEqual(moved.membership, self.other)
        self.assertEqual(PushDevice.objects.count(), 1)
        # Whoever merely knows the endpoint brings other keys: refused.
        with self.assertRaisesMessage(devices.DeviceRefused, "Cet appareil est déjà inscrit."):
            devices.register(self.member, **{**values, "p256dh": new_keys()[0]})
        self.assertEqual(PushDevice.objects.get().membership, self.other)

    def test_a_device_tombstoned_or_logged_out_comes_back(self):
        values = self.subscription()
        device = devices.register(self.member, **values)
        PushDevice.objects.filter(pk=device.pk).update(gone_at=timezone.now(), logged_out_at=timezone.now())
        device = devices.register(self.member, **values)
        self.assertIsNone(device.gone_at)
        self.assertIsNone(device.logged_out_at)

    def test_what_is_no_subscription_is_refused_in_french(self):
        for values, said in (
            (self.subscription(endpoint="https://push.example.invalid/x"), webpush.ENDPOINT_UNKNOWN),
            (self.subscription(endpoint="http://fcm.googleapis.com/x"), webpush.ENDPOINT_INVALID),
            ({**self.subscription(), "auth": "court"}, webpush.KEYS_INVALID),
            (self.subscription(server_key="BUne-ancienne-cle"), devices.KEY_CHANGED),
            ({**self.subscription(), "server_key": None}, devices.KEY_CHANGED),
        ):
            with self.subTest(said=said), self.assertRaisesMessage(devices.DeviceRefused, said):
                devices.register(self.member, **values)
        self.assertFalse(PushDevice.objects.exists())

    def test_the_endpoint_is_stored_as_checked(self):
        values = self.subscription(endpoint="https://FCM.googleapis.com:443/fcm/send/jeton-invente-majuscules")
        device = devices.register(self.member, **values)
        self.assertEqual(device.endpoint, "https://fcm.googleapis.com/fcm/send/jeton-invente-majuscules")

    def test_ten_devices_per_login_the_oldest_seen_goes(self):
        now = timezone.now()
        oldest = make_device(self.member, seen_at=now - timedelta(days=30))
        for number in range(9):
            make_device(self.member, seen_at=now - timedelta(days=number))
        theirs = make_device(self.other, seen_at=now - timedelta(days=60))
        device = devices.register(self.member, **self.subscription())
        mine = PushDevice.objects.filter(membership=self.member)
        self.assertEqual(mine.count(), devices.MAX_DEVICES)
        self.assertIn(device, mine)
        self.assertFalse(PushDevice.objects.filter(pk=oldest.pk).exists())
        self.assertTrue(PushDevice.objects.filter(pk=theirs.pk).exists())


class SyncTests(Logins):
    def test_the_cookie_s_device_is_refreshed_even_on_a_new_endpoint(self):
        device = make_device(self.member, logged_out_at=timezone.now(), seen_at=timezone.now() - timedelta(days=3))
        values = self.subscription()
        synced = devices.sync(self.member, cookie_device_pk=device.pk, **values)
        self.assertEqual(synced.state, devices.OK)
        device.refresh_from_db()
        self.assertEqual(
            (device.endpoint, device.p256dh, device.auth), (values["endpoint"], values["p256dh"], values["auth"])
        )
        self.assertIsNone(device.logged_out_at)
        self.assertGreater(device.seen_at, timezone.now() - timedelta(minutes=1))

    def test_without_a_cookie_by_its_endpoint_among_mine(self):
        device = make_device(self.member)
        synced = devices.sync(
            self.member, endpoint=device.endpoint, p256dh=device.p256dh, auth=device.auth, server_key=self.key
        )
        self.assertEqual((synced.state, synced.device), (devices.OK, device))

    def test_never_creates(self):
        synced = devices.sync(self.member, **self.subscription())
        self.assertEqual(synced, devices.Synced(devices.UNKNOWN))
        self.assertFalse(PushDevice.objects.exists())

    def test_another_login_s_device_is_unknown_here(self):
        theirs = make_device(self.other)
        # Its cookie, or its endpoint: neither reaches it from this login.
        self.assertEqual(devices.sync(self.member, cookie_device_pk=theirs.pk, endpoint=None).state, devices.UNKNOWN)
        synced = devices.sync(
            self.member, endpoint=theirs.endpoint, p256dh=theirs.p256dh, auth=theirs.auth, server_key=self.key
        )
        self.assertEqual(synced.state, devices.UNKNOWN)
        # Nor does the cookie's device take another login's endpoint.
        mine = make_device(self.member)
        synced = devices.sync(
            self.member,
            cookie_device_pk=mine.pk,
            endpoint=theirs.endpoint,
            p256dh=theirs.p256dh,
            auth=theirs.auth,
            server_key=self.key,
        )
        self.assertEqual(synced.state, devices.UNKNOWN)
        theirs.refresh_from_db()
        self.assertEqual(theirs.membership, self.other)

    def test_renew_when_the_browser_must_subscribe_again(self):
        device = make_device(self.member)
        same = {"endpoint": device.endpoint, "p256dh": device.p256dh, "auth": device.auth}
        # No subscription left.
        self.assertEqual(devices.sync(self.member, cookie_device_pk=device.pk, endpoint=None).state, devices.RENEW)
        # Subscribed with another key: never recorded as current.
        stale = devices.sync(self.member, cookie_device_pk=device.pk, server_key="BUne-ancienne-cle", **same)
        self.assertEqual(stale.state, devices.RENEW)
        # Tombstoned by its push service.
        PushDevice.objects.filter(pk=device.pk).update(gone_at=timezone.now())
        self.assertEqual(
            devices.sync(self.member, cookie_device_pk=device.pk, server_key=self.key, **same).state, devices.RENEW
        )
        device.refresh_from_db()
        self.assertEqual(device.server_key, self.key)
        self.assertIsNotNone(device.gone_at)

    def test_a_tombstoned_device_renewed_by_a_sync_is_sent_to_again(self):
        # The recovery the RENEW answer exists for: a 410, then the browser
        # subscribes again and syncs the fresh endpoint. Both ways in: the
        # dead endpoint still reported, or no subscription left at all.
        for dead_endpoint_posted in (True, False):
            with self.subTest(dead_endpoint_posted=dead_endpoint_posted):
                PushDevice.objects.all().delete()
                device = make_device(
                    self.member,
                    gone_at=timezone.now(),
                    failures=4,
                    last_error="le service a oublié cet abonnement",
                )
                old = (
                    {"endpoint": device.endpoint, "p256dh": device.p256dh, "auth": device.auth, "server_key": self.key}
                    if dead_endpoint_posted
                    else {"endpoint": None}
                )
                self.assertEqual(devices.sync(self.member, cookie_device_pk=device.pk, **old).state, devices.RENEW)
                fresh = self.subscription()
                synced = devices.sync(self.member, cookie_device_pk=device.pk, **fresh)
                self.assertEqual(synced.state, devices.OK)
                device.refresh_from_db()
                self.assertIsNone(device.gone_at)
                self.assertEqual((device.failures, device.last_error), (0, ""))
                self.assertFalse(devices.needs_renewal(device))
                self.assertEqual(sending.devices_for(), [device])
                # The next sync of that endpoint is an ordinary one, not a
                # second renewal.
                again = devices.sync(self.member, cookie_device_pk=device.pk, **fresh)
                self.assertEqual(again.state, devices.OK)

    def test_a_sync_on_the_same_endpoint_keeps_the_failures_counted(self):
        device = make_device(self.member, failures=2, last_error="délai dépassé")
        same = {"endpoint": device.endpoint, "p256dh": device.p256dh, "auth": device.auth, "server_key": self.key}
        self.assertEqual(devices.sync(self.member, cookie_device_pk=device.pk, **same).state, devices.OK)
        device.refresh_from_db()
        self.assertEqual((device.failures, device.last_error), (2, "délai dépassé"))

    def test_a_second_row_of_mine_on_that_endpoint_merges(self):
        named = make_device(self.member)
        other_row = make_device(self.member)
        synced = devices.sync(
            self.member,
            cookie_device_pk=named.pk,
            endpoint=other_row.endpoint,
            p256dh=other_row.p256dh,
            auth=other_row.auth,
            server_key=self.key,
        )
        self.assertEqual((synced.state, synced.device.pk), (devices.OK, named.pk))
        self.assertEqual(list(PushDevice.objects.values_list("pk", flat=True)), [named.pk])

    def test_a_foreign_endpoint_or_bad_keys_are_refused(self):
        device = make_device(self.member)
        with self.assertRaises(devices.DeviceRefused):
            devices.sync(self.member, cookie_device_pk=device.pk, endpoint="https://evil.example.invalid/x")
        with self.assertRaises(devices.DeviceRefused):
            devices.sync(self.member, cookie_device_pk=device.pk, endpoint=device.endpoint, p256dh="x", auth="y")

    def test_the_cookie_in_the_request(self):
        device = make_device(self.member)
        request = request_with(cookie_value(device))
        self.assertEqual(devices.cookie_device(request, self.member), device)
        self.assertIsNone(devices.cookie_device(request, self.other))
        request.user = self.member.user
        self.assertEqual(devices.cookie_device_pk(request), device.pk)
        request.user = self.other.user
        self.assertIsNone(devices.cookie_device_pk(request))


class RemoveTests(Logins):
    def test_only_my_own(self):
        mine, theirs = make_device(self.member), make_device(self.other)
        self.assertFalse(devices.remove(self.member, theirs.pk))
        self.assertTrue(devices.remove(self.member, mine.pk))
        self.assertEqual(list(PushDevice.objects.all()), [theirs])

    def test_mine_last_seen_first_and_which_to_reactivate(self):
        now = timezone.now()
        old = make_device(self.member, seen_at=now - timedelta(days=1), server_key="BAutre")
        new = make_device(self.member, seen_at=now)
        self.assertEqual(devices.devices_of(self.member), [new, old])
        self.assertFalse(devices.needs_renewal(new))
        self.assertTrue(devices.needs_renewal(old))
        self.assertTrue(devices.needs_renewal(make_device(self.member, gone_at=now)))
        self.assertTrue(devices.needs_renewal(make_device(self.member, logged_out_at=now)))


class MembershipTests(Logins):
    def test_the_login_in_the_espace_the_request_is_bound_to(self):
        request = request_with(user=self.member.user)
        request.tenant = TEST_TENANT
        self.assertEqual(devices.membership_of(request), self.member)
        elsewhere = Tenant.objects.create(name="Bar Ailleurs", dir_name="bar-ailleurs-essai")
        request.tenant = elsewhere
        self.assertIsNone(devices.membership_of(request))


class OriginTests(TestCase):
    def test_from_the_site_s_address_only_where_it_sends(self):
        request = request_with(host="127.0.0.1:8765")
        self.assertEqual(devices.origin_refusal(request), "")
        with override_settings(SITE_URL=SITE, ALLOWED_HOSTS=["127.0.0.1", "bar-des-tests.example.invalid"]):
            self.assertEqual(
                devices.origin_refusal(request),
                f"Activez les notifications depuis {SITE} : les liens des notifications ouvrent cette adresse.",
            )
            self.assertEqual(devices.origin_refusal(request_with(host="bar-des-tests.example.invalid")), "")


class LogoutTests(Logins):
    """« Se déconnecter » marks the browser's device first (accounts/pages.py)."""

    def setUp(self):
        super().setUp()
        self.owner = Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK)
        self.device = make_device(self.owner)
        self.client.cookies[devices.COOKIE_NAME] = cookie_value(self.device)

    def test_the_named_device_of_this_login_is_marked_and_the_cookie_stays(self):
        theirs = make_device(self.member)
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)
        self.device.refresh_from_db()
        theirs.refresh_from_db()
        self.assertIsNotNone(self.device.logged_out_at)
        self.assertIsNone(theirs.logged_out_at)
        self.assertNotIn(devices.COOKIE_NAME, response.cookies)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_cookie_of_another_login_marks_nothing(self):
        theirs = make_device(self.member)
        self.client.cookies[devices.COOKIE_NAME] = cookie_value(theirs)
        self.client.post(reverse("accounts:logout"))
        theirs.refresh_from_db()
        self.assertIsNone(theirs.logged_out_at)

    def test_a_missing_table_never_stops_a_logout(self):
        with mock.patch.object(PushDevice.objects, "filter", side_effect=DatabaseError("no such table")):
            response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


PASSWORD = "mot-de-passe-des-essais-42"


class AnotherLoginTests(Logins):
    """A login on a browser that carries another login's device cookie
    deletes that device (notifications/signals.py)."""

    def setUp(self):
        super().setUp()
        self.member.user.set_password(PASSWORD)
        self.member.user.save()
        self.browser = Client()

    def log_in(self, user):
        self.browser.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
        self.assertEqual(self.browser.session.get("_auth_user_id"), str(user.pk))

    def test_another_login_s_device_goes(self):
        theirs = make_device(self.other)
        self.browser.cookies[devices.COOKIE_NAME] = cookie_value(theirs)
        self.log_in(self.member.user)
        self.assertFalse(PushDevice.objects.filter(pk=theirs.pk).exists())

    def test_my_own_device_stays(self):
        mine = make_device(self.member, logged_out_at=timezone.now())
        self.browser.cookies[devices.COOKIE_NAME] = cookie_value(mine)
        self.log_in(self.member.user)
        self.assertTrue(PushDevice.objects.filter(pk=mine.pk).exists())

    def test_no_cookie_no_query_and_a_missing_table_never_stops_a_login(self):
        make_device(self.other)
        self.log_in(self.member.user)
        self.assertEqual(PushDevice.objects.count(), 1)
        self.browser.logout()
        self.browser.cookies[devices.COOKIE_NAME] = cookie_value(make_device(self.other))
        with mock.patch.object(PushDevice.objects, "filter", side_effect=DatabaseError("no such table")):
            self.log_in(self.member.user)

    def test_the_receiver_is_wired_by_the_app(self):
        from django.contrib.auth.signals import user_logged_in

        keys = [entry[0][0] for entry in user_logged_in.receivers]
        self.assertIn("notifications.forget_another_login_s_device", keys)
