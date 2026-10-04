"""The device endpoints (notifications/views.py): the key, « Activer »
(`subscribe`), the pages' sync, « Retirer » and « Envoyer un essai » - JSON
in and out, CSRF through X-CSRFToken. They wrap notifications/devices.py and
sending.send_test; what is checked here is the HTTP side: answers, status
codes, the cookie, who may touch which device. Endpoints and keys are
invented; nothing reaches a push service (`push_service` answers)."""

from __future__ import annotations

import json
from unittest import mock

from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, PushDevice
from notifications import devices, webpush
from notifications.models import Dispatch, Reminder
from notifications.tests.support import (
    PRODUCTION,
    SITE,
    QuietLogs,
    make_device,
    make_login,
    new_endpoint,
    new_keys,
    push_service,
)
from tests.runner import TEST_TENANT_PK, test_user

KEY = reverse("notifications:key")
SUBSCRIBE = reverse("notifications:subscribe")
SYNC = reverse("notifications:sync")
TEST = reverse("notifications:test")
HOME = reverse("notifications:home")
ANDROID = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Mobile Safari/537.36"
)


def subscription(endpoint=None, keys=None, server_key=None) -> dict:
    p256dh, auth = keys or new_keys()
    return {
        "endpoint": endpoint or new_endpoint(),
        "keys": {"p256dh": p256dh, "auth": auth},
        "server_key": webpush.vapid_public_key() if server_key is None else server_key,
    }


class ApiCase(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = Membership.objects.get(user=test_user(), tenant_id=TEST_TENANT_PK)

    def post_json(self, url, data, **extra):
        body = data if isinstance(data, (str, bytes)) else json.dumps(data)
        return self.client.post(url, body, content_type="application/json", **extra)

    def cookie_names(self, response):
        return response.cookies.get(devices.COOKIE_NAME)


class KeyTests(ApiCase):
    def test_the_key_and_whether_this_server_sends(self):
        answer = self.client.get(KEY).json()
        self.assertEqual(answer, {"key": webpush.vapid_public_key(), "enabled": False})
        with PRODUCTION:
            self.assertTrue(self.client.get(KEY).json()["enabled"])

    def test_never_cached(self):
        self.assertIn("no-store", self.client.get(KEY)["Cache-Control"])


class SubscribeTests(ApiCase):
    def test_activer_creates_the_device_and_names_it_in_the_cookie(self):
        sent = subscription()
        response = self.post_json(SUBSCRIBE, sent, HTTP_USER_AGENT=ANDROID)
        self.assertEqual(response.status_code, 200)
        device = PushDevice.objects.get()
        self.assertEqual(response.json(), {"ok": True, "device": device.pk})
        self.assertEqual(device.membership, self.owner)
        self.assertEqual(device.label, "Android · Chrome")
        self.assertEqual(device.endpoint, sent["endpoint"])
        cookie = response.cookies[devices.COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_unreadable_bodies_are_400(self):
        for body in ("pas du json", "[1, 2]", json.dumps({"endpoint": 12})):
            with self.subTest(body=body):
                response = self.post_json(SUBSCRIBE, body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["ok"], False)
        self.assertFalse(PushDevice.objects.exists())

    def test_a_body_over_4_kb_is_400(self):
        sent = subscription()
        sent["padding"] = "x" * 5000
        response = self.post_json(SUBSCRIBE, sent)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Demande trop longue.")
        self.assertFalse(PushDevice.objects.exists())

    def test_refusals_say_why(self):
        foreign = subscription(endpoint="https://push.ailleurs.example/x")
        stale_key = subscription(server_key="BAutreCle")
        bad_keys = subscription(keys=("pas-une-cle", "court"))
        for sent in (foreign, stale_key, bad_keys):
            with self.subTest(endpoint=sent["endpoint"]):
                response = self.post_json(SUBSCRIBE, sent)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["error"])
        self.assertEqual(self.post_json(SUBSCRIBE, stale_key).json()["error"], devices.KEY_CHANGED)
        self.assertFalse(PushDevice.objects.exists())

    def test_an_endpoint_registered_with_other_keys_is_refused(self):
        device = make_device(self.owner)
        response = self.post_json(SUBSCRIBE, subscription(endpoint=device.endpoint))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], devices.ALREADY_REGISTERED)

    def test_another_address_than_site_url_is_refused_on_a_sending_server(self):
        with override_settings(SITE_URL=SITE, ALLOWED_HOSTS=["testserver", "bar-des-tests.example.invalid"]):
            response = self.post_json(SUBSCRIBE, subscription())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.json()["error"],
                f"Activez les notifications depuis {SITE} : les liens des notifications ouvrent cette adresse.",
            )
            self.assertFalse(PushDevice.objects.exists())
            response = self.post_json(SUBSCRIBE, subscription(), HTTP_HOST="bar-des-tests.example.invalid")
            self.assertEqual(response.status_code, 200)

    def test_ten_devices_at_most(self):
        for _ in range(devices.MAX_DEVICES + 1):
            self.assertEqual(self.post_json(SUBSCRIBE, subscription()).status_code, 200)
        self.assertEqual(PushDevice.objects.filter(membership=self.owner).count(), devices.MAX_DEVICES)

    def test_a_get_goes_to_the_page_and_writes_nothing(self):
        response = self.client.get(SUBSCRIBE)
        self.assertRedirects(response, f"{HOME}#cet-appareil", fetch_redirect_response=False)
        self.assertFalse(PushDevice.objects.exists())


class SyncTests(ApiCase):
    def name_in_the_cookie(self, device):
        self.client.cookies[devices.COOKIE_NAME] = (
            devices.set_cookie(HttpResponse(), device).cookies[devices.COOKIE_NAME].value
        )

    def test_the_cookie_s_device_is_refreshed(self):
        device = make_device(self.owner, logged_out_at=timezone.now())
        self.name_in_the_cookie(device)
        sent = subscription(endpoint=device.endpoint, keys=(device.p256dh, device.auth))
        response = self.post_json(SYNC, sent)
        self.assertEqual(response.json(), {"state": "ok", "device": device.pk})
        device.refresh_from_db()
        self.assertIsNone(device.logged_out_at)
        self.assertIn(devices.COOKIE_NAME, response.cookies)

    def test_found_by_its_endpoint_without_a_cookie(self):
        device = make_device(self.owner)
        sent = subscription(endpoint=device.endpoint, keys=(device.p256dh, device.auth))
        self.assertEqual(self.post_json(SYNC, sent).json()["state"], "ok")

    def test_renew_when_the_browser_lost_its_subscription_or_holds_another_key(self):
        device = make_device(self.owner)
        self.name_in_the_cookie(device)
        self.assertEqual(self.post_json(SYNC, {"endpoint": None}).json(), {"state": "renew"})
        stale = subscription(endpoint=device.endpoint, keys=(device.p256dh, device.auth), server_key="BAutreCle")
        self.assertEqual(self.post_json(SYNC, stale).json(), {"state": "renew"})

    def test_unknown_never_creates(self):
        self.assertEqual(self.post_json(SYNC, subscription()).json(), {"state": "unknown"})
        self.assertEqual(self.post_json(SYNC, {"endpoint": None}).json(), {"state": "unknown"})
        self.assertFalse(PushDevice.objects.exists())

    def test_another_login_s_device_is_unknown_here(self):
        other = make_login(TEST_TENANT_PK, "collegue@example.invalid")
        device = make_device(other)
        sent = subscription(endpoint=device.endpoint, keys=(device.p256dh, device.auth))
        self.assertEqual(self.post_json(SYNC, sent).json(), {"state": "unknown"})
        device.refresh_from_db()
        self.assertEqual(device.membership, other)

    def test_a_foreign_endpoint_is_400(self):
        response = self.post_json(SYNC, subscription(endpoint="http://fcm.googleapis.com/x"))
        self.assertEqual(response.status_code, 400)


class RemoveTests(ApiCase):
    def test_desactiver_deletes_this_device_and_clears_the_cookie(self):
        answer = self.post_json(SUBSCRIBE, subscription())
        device_pk = answer.json()["device"]
        response = self.post_json(reverse("notifications:device_delete", args=[device_pk]), {})
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(PushDevice.objects.exists())
        self.assertEqual(response.cookies[devices.COOKIE_NAME].value, "")

    def test_retirer_from_the_list_answers_on_the_page(self):
        device = make_device(self.owner)
        response = self.client.post(reverse("notifications:device_delete", args=[device.pk]), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], f"{HOME}#cet-appareil")
        self.assertIn("Appareil retiré.", [str(m) for m in response.context["messages"]])
        self.assertFalse(PushDevice.objects.exists())

    def test_another_login_s_device_is_a_404(self):
        other = make_login(TEST_TENANT_PK, "collegue@example.invalid")
        device = make_device(other)
        self.assertEqual(self.client.post(reverse("notifications:device_delete", args=[device.pk])).status_code, 404)
        self.assertTrue(PushDevice.objects.filter(pk=device.pk).exists())

    def test_a_get_writes_nothing(self):
        device = make_device(self.owner)
        response = self.client.get(reverse("notifications:device_delete", args=[device.pk]))
        self.assertRedirects(response, f"{HOME}#cet-appareil", fetch_redirect_response=False)
        self.assertTrue(PushDevice.objects.filter(pk=device.pk).exists())


class TrialTests(ApiCase):
    def test_a_dev_server_says_it_sends_nothing(self):
        make_device(self.owner)
        answer = self.post_json(TEST, {}).json()
        self.assertEqual(
            answer, {"ok": False, "message": "Envois désactivés sur ce serveur (serveur de développement)."}
        )

    @PRODUCTION
    def test_sent_to_my_device(self):
        device = make_device(self.owner)
        with push_service(201) as service:
            answer = self.post_json(TEST, {"appareil": device.pk}).json()
        self.assertEqual(answer, {"ok": True, "message": "Essai envoyé à 1 appareil."})
        self.assertEqual(service.urls, [device.endpoint])
        self.assertEqual(Dispatch.objects.get().kind, Dispatch.Kind.TEST)

    @PRODUCTION
    def test_no_device_said(self):
        answer = self.post_json(TEST, {}).json()
        self.assertEqual(answer, {"ok": False, "message": "Aucun appareil inscrit."})

    def test_a_reminder_s_texts_from_its_form(self):
        reminder = Reminder.objects.create(
            name="Vides", title="Consignes", body="Comptez.", target="/consignes/", weekdays="5", times="18:00"
        )
        with mock.patch("notifications.sending.send_test", return_value=(1, 1, "")) as sent:
            response = self.client.post(TEST, {"rappel": str(reminder.pk)}, follow=True)
        self.assertEqual(
            sent.call_args.kwargs, {"title": "Consignes", "body": "Comptez.", "target": "/consignes/", "device": None}
        )
        self.assertEqual(response.redirect_chain[-1][0], f"{reverse('notifications:reminders')}#rappel-{reminder.pk}")
        self.assertIn("Essai envoyé à 1 appareil.", [str(m) for m in response.context["messages"]])

    def test_ids_that_are_none_of_mine_are_404(self):
        other = make_login(TEST_TENANT_PK, "collegue@example.invalid")
        device = make_device(other)
        with mock.patch("notifications.sending.send_test") as sent:
            for data in ({"appareil": device.pk}, {"appareil": "²"}, {"rappel": 999}, {"rappel": "abc"}):
                with self.subTest(data=data):
                    self.assertEqual(self.post_json(TEST, data).status_code, 404)
        sent.assert_not_called()

    def test_a_get_writes_nothing(self):
        response = self.client.get(TEST)
        self.assertRedirects(response, f"{HOME}#cet-appareil", fetch_redirect_response=False)
        self.assertFalse(Dispatch.objects.exists())


class CsrfJsonTests(ApiCase):
    """The script posts JSON with the csrftoken cookie's value in
    X-CSRFToken: accepted; without it, refused."""

    def test_the_header_is_what_lets_a_post_through(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.get(HOME)
        token = client.cookies["csrftoken"].value
        body = json.dumps(subscription())
        refused = client.post(SUBSCRIBE, body, content_type="application/json")
        self.assertEqual(refused.status_code, 403)
        accepted = client.post(SUBSCRIBE, body, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(PushDevice.objects.count(), 1)

    def test_a_form_posted_with_the_header_is_refused_in_json_not_a_500(self):
        # CSRF enforced: its middleware reads a multipart body from the
        # stream first, and the view can no longer read it (a 500 before).
        client = self.client_class(enforce_csrf_checks=True, raise_request_exception=False)
        client.get(HOME)
        token = client.cookies["csrftoken"].value
        for url in (SUBSCRIBE, SYNC):
            for data, content_type in (
                ({"endpoint": new_endpoint()}, None),
                ("endpoint=x", "application/x-www-form-urlencoded"),
                (json.dumps(subscription()), "text/plain"),
            ):
                with self.subTest(url=url, content_type=content_type or "multipart/form-data"):
                    extra = {"content_type": content_type} if content_type else {}
                    answer = client.post(url, data, HTTP_X_CSRFTOKEN=token, **extra)
                    self.assertEqual(answer.status_code, 400)
                    self.assertEqual(answer.json(), {"ok": False, "error": "Demande illisible."})
        self.assertFalse(PushDevice.objects.exists())

    def test_json_with_a_charset_is_still_json(self):
        answer = self.client.post(SUBSCRIBE, json.dumps(subscription()), content_type="application/json; charset=utf-8")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(PushDevice.objects.count(), 1)


class TwoBarsTests(ApiCase):
    """A device of another espace is never this espace's."""

    def test_a_device_of_another_bar_is_unknown_and_untouchable(self):
        from accounts.models import Tenant

        other_bar = Tenant.objects.create(name="Bar voisin", dir_name="bar-voisin-essai")
        stranger = make_login(other_bar.pk, "voisin@example.invalid")
        device = make_device(stranger)
        sent = subscription(endpoint=device.endpoint, keys=(device.p256dh, device.auth))
        self.assertEqual(self.post_json(SYNC, sent).json(), {"state": "unknown"})
        self.assertEqual(self.client.post(reverse("notifications:device_delete", args=[device.pk])).status_code, 404)
        self.assertTrue(PushDevice.objects.filter(pk=device.pk).exists())
