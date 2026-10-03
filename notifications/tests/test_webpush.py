"""The push transport (notifications/webpush.py): RFC 8291's test vector byte
for byte, RFC 8292's example token, the allowlist, the answers mapped to
fixed French errors, and nothing sent from a development copy. Nothing here
reaches the network: a test that wants an answer passes `post=`, and the
run's own guard (tests/runner.py) stands behind every other."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
from unittest import mock

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.test import SimpleTestCase, override_settings

from notifications import webpush
from tests.support import ForbiddenNetworkCall, _Forbidden

# RFC 8291, Appendix A (and §5 for the body), base64url.
RFC8291 = {
    "plaintext": "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24",
    "as_public": "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "auth_secret": "BTBZMqHH6r4Tts7J_aSIgg",
    "header": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
    "ciphertext": "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ",
    "body": (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    ),
}

# RFC 8292, §2.4: a token and the key it verifies with.
RFC8292_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJhdWQiOiJodHRwczovL3"
    "B1c2guZXhhbXBsZS5uZXQiLCJleHAiOjE0NTM1MjM3NjgsInN1YiI6Im1ha"
    "Wx0bzpwdXNoQGV4YW1wbGUuY29tIn0.i3CYb7t4xfxCDquptFOepC9GAu_H"
    "LGkMlMuCGSK2rpiUfnK9ojFwDXb1JrErtmysazNjjvW2L9OkSSHzvoD1oA"
)
RFC8292_KEY = "BA1Hxzyi1RUM1b5wjxsn7nGxAszw2u61m164i3MrAIxHF6YK5h4SDYic-dRuU_RCPCfA5aq9ojSwk5Y2EmClBPs"

#: Invented: a device's token on Chrome's push service.
TOKEN = "dGVzdC10b2tlbi1pbnZlbnRlLXBvdXItbGVzLXRlc3Rz"
ENDPOINT = f"https://fcm.googleapis.com/fcm/send/{TOKEN}"
SITE = "https://bar-des-tests.example.invalid"
STRONG_KEY = "cle-des-tests-de-notifications-0123456789-abcdefghijklmnopqrstuvwxyz"
OTHER_STRONG_KEY = "autre-cle-des-tests-de-notifications-9876543210-ZYXWVUTSRQPONMLKJ"
NOW = 1_900_000_000

#: What makes `sending_enabled()` hold: a production server.
PRODUCTION = {"SITE_URL": SITE, "DEBUG": False, "SECRET_KEY": STRONG_KEY}


def decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_device():
    """A browser's subscription keys, made now: (private key, p256dh, auth)."""
    private = ec.generate_private_key(ec.SECP256R1())
    point = webpush._public_bytes(private)
    return private, webpush.b64u_encode(point), webpush.b64u_encode(b"0123456789abcdef")


def decrypt(body: bytes, ua_private, auth_secret: bytes) -> bytes:
    """The browser's side of RFC 8291, to read back what `encrypt` sealed."""
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_public = body[21 : 21 + idlen]
    ua_public = webpush._public_bytes(ua_private)
    secret = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(secret)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
    assert rs == webpush.RECORD_SIZE
    assert record.endswith(b"\x02")
    return record[:-1]


def jwt_parts(authorization: str):
    """(header, claims, signing input, raw signature, k) of a `vapid t=…, k=…`."""
    assert authorization.startswith("vapid t=")
    token, key = authorization[len("vapid t=") :].split(", k=")
    header, claims, signature = token.split(".")
    return (
        json.loads(decode(header)),
        json.loads(decode(claims)),
        f"{header}.{claims}".encode(),
        decode(signature),
        key,
    )


def verifies(signing_input: bytes, raw_signature: bytes, k: str) -> bool:
    """ES256: the raw r‖s turned back into DER, checked with `k`."""
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(k))
    der = encode_dss_signature(int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big"))
    try:
        public.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except Exception:  # noqa: BLE001 - InvalidSignature is the answer, not an error
        return False
    return True


class _Answer:
    def __init__(self, status):
        self.status_code = status


def answering(*statuses):
    """A `post` returning these statuses (or raising these exceptions) in turn."""
    effects = [s if isinstance(s, BaseException) else _Answer(s) for s in statuses]
    return mock.Mock(side_effect=effects)


class ForgetTokens(SimpleTestCase):
    """Every test starts with no cached JWT: the cache is the process's."""

    def setUp(self):
        super().setUp()
        with webpush._lock:
            webpush._tokens.clear()


class Base64UrlTests(SimpleTestCase):
    def test_round_trip_without_padding(self):
        for data in (b"", b"a", b"ab", b"abc", bytes(range(256))):
            text = webpush.b64u_encode(data)
            self.assertNotIn("=", text)
            self.assertEqual(webpush.b64u_decode(text), data)

    def test_padding_is_accepted(self):
        self.assertEqual(webpush.b64u_decode("YQ=="), b"a")

    def test_foreign_characters_are_refused_not_skipped(self):
        for text in ("a+b/", "YW Jj", "YWJj\n", "é", "YQ==="):
            with self.subTest(text=text), self.assertRaises(ValueError):
                webpush.b64u_decode(text)


class Rfc8291VectorTests(SimpleTestCase):
    """RFC 8291 Appendix A, byte for byte: raw bytes are compared, since the
    86-byte header's base64 does not end on a group of three."""

    def test_the_appendix_a_body_is_reproduced_exactly(self):
        as_private = ec.derive_private_key(int.from_bytes(decode(RFC8291["as_private"]), "big"), ec.SECP256R1())
        body = webpush.encrypt(
            decode(RFC8291["plaintext"]),
            decode(RFC8291["ua_public"]),
            decode(RFC8291["auth_secret"]),
            salt=decode(RFC8291["salt"]),
            as_private=as_private,
        )
        self.assertEqual(body, decode(RFC8291["body"]))
        self.assertEqual(body[:86], decode(RFC8291["header"]))
        self.assertEqual(body[86:], decode(RFC8291["ciphertext"]))
        # §5 prints 145: erratum 5230, the real body is 144 bytes.
        self.assertEqual(len(body), 144)
        self.assertEqual(webpush._public_bytes(as_private), decode(RFC8291["as_public"]))

    def test_a_message_is_read_back_by_the_browser_side(self):
        private, p256dh, auth = new_device()
        body = webpush.encrypt(b"Consignes", decode(p256dh), decode(auth))
        self.assertEqual(len(body), 86 + len(b"Consignes") + 1 + 16)
        self.assertEqual(decrypt(body, private, decode(auth)), b"Consignes")

    def test_each_message_has_its_own_salt_and_key(self):
        _, p256dh, auth = new_device()
        first = webpush.encrypt(b"x", decode(p256dh), decode(auth))
        second = webpush.encrypt(b"x", decode(p256dh), decode(auth))
        self.assertNotEqual(first[:16], second[:16])
        self.assertNotEqual(first[21:86], second[21:86])

    def test_more_than_one_record_holds_is_refused(self):
        _, p256dh, auth = new_device()
        webpush.encrypt(b"x" * webpush.MAX_PLAINTEXT, decode(p256dh), decode(auth))
        self.assertEqual(webpush.MAX_PLAINTEXT, 3993)
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), decode(p256dh), decode(auth))


@override_settings(SITE_URL=SITE, SECRET_KEY=STRONG_KEY)
class VapidTests(ForgetTokens):
    def test_the_rfc_8292_example_token_verifies_with_its_key(self):
        header, claims, signing_input, signature, _ = jwt_parts(f"vapid t={RFC8292_TOKEN}, k={RFC8292_KEY}")
        self.assertEqual(header, {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(claims["aud"], "https://push.example.net")
        self.assertEqual(len(signature), 64)
        self.assertTrue(verifies(signing_input, signature, RFC8292_KEY))
        self.assertFalse(verifies(signing_input + b"x", signature, RFC8292_KEY))

    def test_the_key_is_derived_from_secret_key(self):
        material = HKDF(hashes.SHA256(), 48, b"marginmate-vapid", b"web-push-vapid-p256-v1").derive(STRONG_KEY.encode())
        scalar = int.from_bytes(material, "big") % (webpush.P256_ORDER - 1) + 1
        self.assertEqual(webpush.vapid_private_key().private_numbers().private_value, scalar)
        public = decode(webpush.vapid_public_key())
        self.assertEqual(len(public), 65)
        self.assertEqual(public[0], 4)
        self.assertIs(webpush.vapid_private_key(), webpush.vapid_private_key())

    def test_another_secret_key_is_another_vapid_key(self):
        first = webpush.vapid_public_key()
        with override_settings(SECRET_KEY=OTHER_STRONG_KEY):
            self.assertNotEqual(webpush.vapid_public_key(), first)
        self.assertEqual(webpush.vapid_public_key(), first)

    def test_a_fresh_token_verifies_with_our_key(self):
        header, claims, signing_input, signature, k = jwt_parts(webpush.vapid_authorization(ENDPOINT, now=NOW))
        self.assertEqual(header, {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(k, webpush.vapid_public_key())
        self.assertEqual(len(signature), 64)
        self.assertTrue(verifies(signing_input, signature, k))
        self.assertEqual(claims, {"aud": "https://fcm.googleapis.com", "exp": NOW + 12 * 3600, "sub": SITE})
        self.assertLessEqual(claims["exp"] - NOW, 24 * 3600)

    def test_the_audience_is_the_endpoint_origin(self):
        cases = {
            "https://push.example.net:8443/x/y?z=1": "https://push.example.net:8443",
            "https://web.push.apple.com:443/abc": "https://web.push.apple.com",
            "https://WEB.push.apple.com/abc": "https://web.push.apple.com",
        }
        for endpoint, aud in cases.items():
            with self.subTest(endpoint=endpoint):
                _, claims, *_ = jwt_parts(webpush.vapid_authorization(endpoint, now=NOW))
                self.assertEqual(claims["aud"], aud)

    @override_settings(SITE_URL="")
    def test_no_site_no_subject(self):
        _, claims, *_ = jwt_parts(webpush.vapid_authorization(ENDPOINT, now=NOW))
        self.assertNotIn("sub", claims)

    def test_a_token_is_reused_for_an_hour_per_audience(self):
        first = webpush.vapid_authorization(ENDPOINT, now=NOW)
        self.assertEqual(webpush.vapid_authorization(ENDPOINT, now=NOW + 3599), first)
        self.assertNotEqual(webpush.vapid_authorization("https://web.push.apple.com/a", now=NOW), first)
        later = webpush.vapid_authorization(ENDPOINT, now=NOW + 3600)
        self.assertNotEqual(later, first)
        _, claims, *_ = jwt_parts(later)
        self.assertEqual(claims["exp"], NOW + 3600 + 12 * 3600)

    def test_a_clock_gone_back_signs_again(self):
        first = webpush.vapid_authorization(ENDPOINT, now=NOW)
        self.assertNotEqual(webpush.vapid_authorization(ENDPOINT, now=NOW - 10), first)

    def test_the_cache_is_per_key(self):
        """Two SECRET_KEYs one after the other: each token verifies with its
        own `k`, never one signed with the other key."""
        first = webpush.vapid_authorization(ENDPOINT, now=NOW)
        with override_settings(SECRET_KEY=OTHER_STRONG_KEY):
            second = webpush.vapid_authorization(ENDPOINT, now=NOW + 1)
        again = webpush.vapid_authorization(ENDPOINT, now=NOW + 2)
        self.assertEqual(again, first)
        for authorization in (first, second):
            _, _, signing_input, signature, k = jwt_parts(authorization)
            self.assertTrue(verifies(signing_input, signature, k))
        self.assertNotEqual(jwt_parts(first)[4], jwt_parts(second)[4])

    def test_a_given_private_key_signs(self):
        key = ec.generate_private_key(ec.SECP256R1())
        _, _, signing_input, signature, k = jwt_parts(webpush.vapid_authorization(ENDPOINT, now=NOW, private_key=key))
        self.assertEqual(k, webpush.b64u_encode(webpush._public_bytes(key)))
        self.assertTrue(verifies(signing_input, signature, k))


class SendingEnabledTests(SimpleTestCase):
    @override_settings(**PRODUCTION)
    def test_a_production_server_sends(self):
        self.assertTrue(webpush.sending_enabled())

    def test_anything_else_does_not(self):
        cases = {
            "no site": {"SITE_URL": ""},
            "http site": {"SITE_URL": "http://bar-des-tests.example.invalid"},
            "debug": {"DEBUG": True},
            "short key": {"SECRET_KEY": "trop-courte"},
            "development key": {"SECRET_KEY": "django-insecure-" + "x1y2z3w4v5" * 6},
        }
        for name, change in cases.items():
            with self.subTest(name), override_settings(**{**PRODUCTION, **change}):
                self.assertFalse(webpush.sending_enabled())


class AllowlistTests(SimpleTestCase):
    GOOD = {
        ENDPOINT: ENDPOINT,
        "https://android.googleapis.com/gcm/send/abc": "https://android.googleapis.com/gcm/send/abc",
        "https://jmt17.google.com/fcm/send/abc": "https://jmt17.google.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAA": (
            "https://updates.push.services.mozilla.com/wpush/v2/gAAA"
        ),
        "https://web.push.apple.com/QGz9": "https://web.push.apple.com/QGz9",
        "https://wns2-par02p.notify.windows.com/w/?token=AQE%3d": "https://wns2-par02p.notify.windows.com/w/?token=AQE%3d",
        # Rebuilt: the default port, the host lower-cased, the fragment dropped.
        "https://fcm.googleapis.com:443/fcm/send/abc": "https://fcm.googleapis.com/fcm/send/abc",
        "https://FCM.googleapis.com/fcm/send/abc": "https://fcm.googleapis.com/fcm/send/abc",
        "https://web.push.apple.com/abc#frag": "https://web.push.apple.com/abc",
    }
    BAD = (
        "https://evil.com\\@fcm.googleapis.com/x",
        "https://fcm.googleapis.com/fcm/send/a\tb",
        "https://fcm.googleapis.com/fcm/send/a b",
        "https://fcm.googleapis.com/fcm/send/a\nb",
        "https://fcm%2egoogleapis.com/x",
        "https://evil.com%2e.push.apple.com/x",
        "https://evil.com%40.push.apple.com/x",
        "https://fcm.googleapis.com:abc/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com:0/x",
        "http://fcm.googleapis.com/x",
        "HTTPS://fcm.googleapis.com/x",
        "https://user@fcm.googleapis.com/x",
        "https://user:pass@fcm.googleapis.com/x",
        "https://fcm.googleapis.com.evil.com/x",
        "https://push.apple.com/x",
        "https://.push.apple.com/x",
        "https://notify.windows.com/x",
        "https://fcm.googleapis.com./x",
        "https://192.0.2.10/x",
        "https://[2001:db8::1]/x",
        "https://xn--fcm.googleapis.com.evil.com/x",
        "https://fcm.googleapis.com\N{IDEOGRAPHIC FULL STOP}evil.com/x",
        "https://\N{FULLWIDTH LATIN SMALL LETTER F}cm.googleapis.com/x",
        "https://fcm.googleapis.com\N{NO-BREAK SPACE}/x",
        "https://evil.com/fcm.googleapis.com",
        "https:fcm.googleapis.com/x",
        "//fcm.googleapis.com/x",
        "",
        "https://" + "a" * 2048 + ".push.apple.com/",
        None,
        42,
    )

    def test_the_known_push_services_pass_rebuilt(self):
        for url, rebuilt in self.GOOD.items():
            with self.subTest(url=url):
                self.assertEqual(webpush.check_endpoint(url), rebuilt)

    def test_anything_else_is_refused_in_french(self):
        for url in self.BAD:
            with self.subTest(url=url), self.assertRaises(webpush.BadEndpoint) as refused:
                webpush.check_endpoint(url)
            self.assertIn(str(refused.exception), {webpush.ENDPOINT_INVALID, webpush.ENDPOINT_UNKNOWN})

    def test_the_longest_endpoint_allowed(self):
        path = "/" + "a" * (webpush.MAX_ENDPOINT_LENGTH - len("https://web.push.apple.com/"))
        url = "https://web.push.apple.com" + path
        self.assertEqual(len(url), webpush.MAX_ENDPOINT_LENGTH)
        self.assertEqual(webpush.check_endpoint(url), url)
        with self.assertRaises(webpush.BadEndpoint):
            webpush.check_endpoint(url + "a")


class KeysTests(SimpleTestCase):
    def test_a_subscription_s_keys_are_decoded(self):
        private, p256dh, auth = new_device()
        point, secret = webpush.check_keys(p256dh, auth)
        self.assertEqual(point, webpush._public_bytes(private))
        self.assertEqual(len(secret), 16)
        self.assertEqual(webpush.check_keys(p256dh + "=", auth + "=="), (point, secret))

    def test_anything_else_is_refused(self):
        _, p256dh, auth = new_device()
        off_the_curve = webpush.b64u_encode(b"\x04" + b"\x01" * 64)
        cases = {
            "not on the curve": (off_the_curve, auth),
            "short point": (webpush.b64u_encode(decode(p256dh)[:64]), auth),
            "short auth": (p256dh, webpush.b64u_encode(b"0" * 15)),
            "long auth": (p256dh, webpush.b64u_encode(b"0" * 17)),
            "not base64url": (p256dh.replace("A", "+"), auth),
            "too long": (p256dh + "A" * 100, auth),
            "not text": (None, auth),
            "empty": ("", ""),
        }
        if "A" not in p256dh:
            cases["not base64url"] = ("+" + p256dh[1:], auth)
        for name, (point, secret) in cases.items():
            with self.subTest(name), self.assertRaises(webpush.BadKeys) as refused:
                webpush.check_keys(point, secret)
            self.assertEqual(str(refused.exception), webpush.KEYS_INVALID)


class PayloadTests(SimpleTestCase):
    URL = SITE + "/consignes/#new-pickup"

    def test_the_declarative_web_push_shape(self):
        data = webpush.payload_for("Consignes", "Comptez les vides.", self.URL, "d12")
        self.assertEqual(
            json.loads(data),
            {
                "web_push": 8030,
                "notification": {
                    "title": "Consignes",
                    "body": "Comptez les vides.",
                    "navigate": self.URL,
                    "tag": "d12",
                    "lang": "fr-FR",
                    "dir": "ltr",
                },
                "mutable": False,
            },
        )
        self.assertNotIn(b": ", data)
        self.assertNotIn(b", ", data)

    def test_accents_are_written_as_utf_8(self):
        data = webpush.payload_for("Écart", "Fûts : 2", self.URL, "d1")
        self.assertIn("Écart".encode(), data)
        self.assertNotIn(b"\\u", data)

    def test_title_and_body_are_clipped(self):
        notification = json.loads(webpush.payload_for("T" * 200, "B" * 1000, self.URL, "d1"))["notification"]
        self.assertEqual(notification["title"], "T" * 79 + "…")
        self.assertEqual(notification["body"], "B" * 399 + "…")

    def test_the_whole_stays_under_2000_bytes(self):
        # Four bytes a character: clipped to its 80 and 400 characters, the
        # title and the body alone are over 1 900 bytes.
        emoji = "\N{GRINNING FACE}"
        data = webpush.payload_for(emoji * 100, emoji * 500, self.URL, "d1")
        self.assertLessEqual(len(data), webpush.PAYLOAD_MAX_BYTES)
        self.assertGreater(len(data), webpush.PAYLOAD_MAX_BYTES - 8)
        notification = json.loads(data)["notification"]
        self.assertEqual(notification["title"], emoji * 79 + "…")
        self.assertTrue(notification["body"].endswith("…"))
        self.assertLess(len(notification["body"]), 400)
        self.assertGreater(len(notification["body"]), 300)

    def test_the_body_goes_before_the_title_and_the_link_is_never_cut(self):
        url = SITE + "/" + "a" * 1800
        notification = json.loads(webpush.payload_for("T" * 80, "B" * 400, url, "d1"))["notification"]
        self.assertEqual(notification["navigate"], url)
        self.assertEqual(notification["body"], "")
        self.assertTrue(notification["title"].endswith("…"))
        with self.assertRaises(ValueError):
            webpush.payload_for("T", "B", SITE + "/" + "a" * 2000, "d1")

    def test_quotes_count_as_what_json_makes_of_them(self):
        data = webpush.payload_for("t", '"' * 400, self.URL, "d1")
        self.assertLessEqual(len(data), webpush.PAYLOAD_MAX_BYTES)

    def test_empty_texts(self):
        notification = json.loads(webpush.payload_for("", None, self.URL, "d1"))["notification"]
        self.assertEqual((notification["title"], notification["body"]), ("", ""))


@override_settings(**PRODUCTION)
class SendTests(ForgetTokens):
    PAYLOAD = b'{"web_push":8030}'

    def setUp(self):
        super().setUp()
        self.private, self.p256dh, self.auth = new_device()
        sleep = mock.patch.object(webpush, "_sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        # The warnings each failed send logs stay off the test run's console
        # (the test settings configure no handler); assertLogs still sees them.
        logger = logging.getLogger("notifications.webpush")
        for quiet in (
            mock.patch.object(logger, "propagate", False),
            mock.patch.object(logger, "handlers", [logging.NullHandler()]),
        ):
            quiet.start()
            self.addCleanup(quiet.stop)

    def send(self, post, **kw):
        kw.setdefault("ttl", webpush.TTL_EVENT)
        return webpush.send(ENDPOINT, self.p256dh, self.auth, self.PAYLOAD, now=NOW, post=post, **kw)

    def test_accepted(self):
        post = answering(201)
        result = self.send(post, topic="r12")
        self.assertEqual(result, webpush.PushResult(ok=True, status=201))
        post.assert_called_once()
        (url,), kwargs = post.call_args
        self.assertEqual(url, ENDPOINT)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], (5, 10))
        headers = kwargs["headers"]
        self.assertEqual(headers["TTL"], "86400")
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Urgency"], "high")
        self.assertEqual(headers["Topic"], "r12")
        _, claims, signing_input, signature, k = jwt_parts(headers["Authorization"])
        self.assertEqual(k, webpush.vapid_public_key())
        self.assertTrue(verifies(signing_input, signature, k))
        self.assertEqual(claims["exp"], NOW + 12 * 3600)
        self.assertEqual(decrypt(kwargs["data"], self.private, decode(self.auth)), self.PAYLOAD)

    def test_any_2xx_is_accepted(self):
        for status in (200, 202, 204):
            with self.subTest(status=status):
                self.assertTrue(self.send(answering(status)).ok)

    def test_no_topic_no_header_and_ttl_never_zero(self):
        post = answering(201)
        self.send(post, ttl=0)
        headers = post.call_args.kwargs["headers"]
        self.assertNotIn("Topic", headers)
        self.assertEqual(headers["TTL"], "1")

    def test_gone(self):
        for status in (404, 410):
            with self.subTest(status=status):
                post = answering(status)
                result = self.send(post)
                self.assertEqual(result, webpush.PushResult(ok=False, status=status, gone=True, error=webpush.GONE))
                post.assert_called_once()

    def test_the_request_refused(self):
        for status in (400, 413):
            with self.subTest(status=status):
                post = answering(status)
                result = self.send(post)
                self.assertEqual(result.error, f"refusé par le service (code {status})")
                self.assertFalse(result.gone)
                post.assert_called_once()

    def test_the_key_refused(self):
        for status in (401, 403):
            with self.subTest(status=status):
                post = answering(status)
                result = self.send(post)
                self.assertEqual(result.error, "clé refusée — réactivez les notifications sur cet appareil")
                post.assert_called_once()

    def test_a_redirect_is_not_followed(self):
        post = answering(301)
        result = self.send(post)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "refusé par le service (code 301)")
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)
        post.assert_called_once()

    def test_busy_or_broken_is_tried_once_more_after_two_seconds(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                post = answering(status, status)
                result = self.send(post)
                self.assertEqual(result.error, f"refusé par le service (code {status})")
                self.assertEqual(post.call_count, 2)
                self.sleep.assert_called_once_with(2)

    def test_the_retry_may_succeed(self):
        post = answering(503, 201)
        self.assertTrue(self.send(post).ok)
        self.assertEqual(post.call_count, 2)

    def test_no_retry_when_asked(self):
        post = answering(503, 201)
        result = self.send(post, retry=False)
        self.assertFalse(result.ok)
        post.assert_called_once()
        self.sleep.assert_not_called()

    def test_network_errors_are_fixed_sentences(self):
        cases = {
            webpush.TIMEOUT_ERROR: requests.Timeout("read timed out"),
            webpush.UNREACHABLE: requests.ConnectionError("refused"),
            webpush.NETWORK_ERROR: requests.RequestException("other"),
        }
        for error, exc in cases.items():
            with self.subTest(error=error):
                post = answering(exc, exc)
                result = self.send(post)
                self.assertEqual(result, webpush.PushResult(ok=False, error=error))
                self.assertEqual(post.call_count, 2)

    def test_a_connect_timeout_is_a_timeout(self):
        exc = requests.ConnectTimeout("connect")
        self.assertEqual(self.send(answering(exc, exc)).error, webpush.TIMEOUT_ERROR)

    def test_the_device_token_reaches_no_error_and_no_log(self):
        """A requests exception carries the whole endpoint - the device's
        bearer token. Neither the result nor any log record may hold it."""
        exc = requests.ConnectionError(
            f"HTTPSConnectionPool(host='fcm.googleapis.com', port=443): Max retries exceeded with url: "
            f"/fcm/send/{TOKEN} (Caused by NewConnectionError)"
        )
        with self.assertLogs("notifications.webpush", "DEBUG") as logs:
            result = self.send(answering(exc, exc))
        self.assertEqual(result.error, webpush.UNREACHABLE)
        for record in logs.records:
            self.assertNotIn(TOKEN, record.getMessage())
            self.assertIsNone(record.exc_info)
        self.assertIn(webpush._where(ENDPOINT), logs.output[0])

    def test_a_bad_endpoint_sends_nothing(self):
        post = answering(201)
        result = webpush.send("https://evil.example.invalid/x", self.p256dh, self.auth, self.PAYLOAD, ttl=1, post=post)
        self.assertEqual(result, webpush.PushResult(ok=False, error=webpush.ENDPOINT_UNKNOWN))
        post.assert_not_called()

    def test_bad_keys_send_nothing(self):
        post = answering(201)
        result = webpush.send(ENDPOINT, "abc", self.auth, self.PAYLOAD, ttl=1, post=post)
        self.assertEqual(result, webpush.PushResult(ok=False, error=webpush.KEYS_INVALID))
        post.assert_not_called()

    def test_our_own_bug_is_an_internal_error_never_raised(self):
        post = answering(201)
        with self.assertLogs("notifications.webpush", "ERROR"):
            bad_topic = self.send(post, topic="a topic with spaces")
            too_big = webpush.send(ENDPOINT, self.p256dh, self.auth, b"x" * 5000, ttl=1, now=NOW, post=post)
        self.assertEqual(bad_topic.error, webpush.INTERNAL_ERROR)
        self.assertEqual(too_big.error, webpush.INTERNAL_ERROR)
        post.assert_not_called()

    def test_a_disabled_server_sends_nothing(self):
        post = answering(201)
        for change in ({"SITE_URL": ""}, {"DEBUG": True}, {"SECRET_KEY": "courte"}):
            with self.subTest(change=change), override_settings(**change):
                result = self.send(post)
                self.assertEqual(result, webpush.PushResult(ok=False, error=webpush.DISABLED))
        post.assert_not_called()

    def test_the_run_wide_guard_stops_a_test_that_forgot_to_mock(self):
        self.assertIsInstance(webpush._post, _Forbidden)
        with self.assertRaises(ForbiddenNetworkCall):
            webpush.send(ENDPOINT, self.p256dh, self.auth, self.PAYLOAD, ttl=1, now=NOW)

    def test_a_test_may_patch_the_entry_point(self):
        with mock.patch.object(webpush, "_post", answering(201)) as post:
            self.assertTrue(webpush.send(ENDPOINT, self.p256dh, self.auth, self.PAYLOAD, ttl=1, now=NOW).ok)
        post.assert_called_once()


class TransportTests(SimpleTestCase):
    def test_the_session_ignores_the_environment_and_is_kept(self):
        session = webpush._session()
        self.assertIsInstance(session, requests.Session)
        self.assertFalse(session.trust_env)
        self.assertIs(webpush._session(), session)

    def test_a_request_never_follows_a_redirect_and_has_a_timeout(self):
        session = mock.Mock()
        with mock.patch.object(webpush, "_session", return_value=session):
            webpush._send_request(ENDPOINT, data=b"x", allow_redirects=True)
        session.post.assert_called_once_with(ENDPOINT, data=b"x", allow_redirects=False, timeout=(5, 10))

    def test_a_device_is_named_by_its_host_and_a_short_hash(self):
        where = webpush._where(ENDPOINT)
        self.assertEqual(where, f"fcm.googleapis.com #{hashlib.sha256(ENDPOINT.encode()).hexdigest()[:8]}")
        self.assertNotIn(TOKEN, where)
        self.assertTrue(webpush._where("not a url").startswith("? #"))
        self.assertTrue(webpush._where(None).startswith("? #"))
