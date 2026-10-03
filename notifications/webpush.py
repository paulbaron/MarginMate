"""Web Push without a library: RFC 8291 (the message encrypted for one
browser, « aes128gcm »), RFC 8292 (VAPID: the server signs each request) and
RFC 8030 (the POST to the browser's push service). pywebpush and py-vapid are
MPL-2.0; the owner wants permissive licences only, and `cryptography` and
`requests` - both already locked - are all this needs.

**The VAPID key is derived from SECRET_KEY** (HKDF-SHA256, its own salt and
info, reduced into [1, n-1] of P-256): nothing to store, nothing in .env.
Each copy has its own SECRET_KEY (refresh_dev_data refuses a backup holding
this folder's key), so a development copy signs with another key than
production and the push services refuse it for production's devices.
Changing SECRET_KEY changes the key: every device must subscribe again
(the page's sync does it, RFC 8292 §4.2).

**Nothing is sent unless `sending_enabled()`**: an https SITE_URL (also the
JWT's `sub` and the base of a notification's link), DEBUG off and a strong
SECRET_KEY - a production server. `send` checks it itself, before anything
else: a development copy never reaches a push service, whatever its caller
forgot.

**One HTTP entry point, `_post`**, looked up at call time: the test run
replaces it with a stub that raises (tests/runner.py `install`), so a test
that forgets to mock fails loudly; a test that wants an answer passes
`post=` to `send` or patches `_post`. Its session ignores the environment's
proxies and never follows a redirect.

**Only the known push services** (`check_endpoint`, at registration AND at
each send): an endpoint is a URL the browser hands us, so it is checked like
any URL from outside (SSRF) - https on 443, a plain host name from the
allowlist, no userinfo, no escape in the authority - and rebuilt from the
checked parts.

**No exception text reaches a page, a row or a log**: a requests exception
carries the whole endpoint, whose path is the device's bearer token. Errors
are fixed French strings by class, and a log names a device by `_where`
(its host and a short hash).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

from config.security import secret_key_problem

logger = logging.getLogger(__name__)

CURVE = ec.SECP256R1()
#: The order n of P-256's base point: a private scalar lies in [1, n-1].
P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
VAPID_SALT = b"marginmate-vapid"
VAPID_INFO = b"web-push-vapid-p256-v1"

#: A JWT is valid 12 hours (RFC 8292: at most 24) and reused for at most an
#: hour: Apple asks not to sign a new one more often.
JWT_LIFETIME = 12 * 3600
JWT_REUSE = 3600

#: RFC 8291: one record of 4096 bytes at most, the push services' guaranteed
#: size, minus the 86-byte header, the delimiter and the 16-byte tag.
RECORD_SIZE = 4096
MAX_PLAINTEXT = RECORD_SIZE - 86 - 1 - 16
#: What a notification carries: Firefox for Android's bridged pushes take
#: about 2.7 KB, so the payload stays under 2 000 bytes.
PAYLOAD_MAX_BYTES = 2000
TITLE_MAX_LENGTH = 80
BODY_MAX_LENGTH = 400
ELLIPSIS = "…"

#: The TTL header by kind, in seconds (never 0: Apple refuses it). A reminder
#: is useless an hour late; an event still says something the next day.
TTL_REMINDER = 3600
TTL_EVENT = 86400
TTL_TEST = 600

#: (connect, read) in seconds, and the wait before the one retry.
TIMEOUT = (5, 10)
RETRY_DELAY = 2

MAX_ENDPOINT_LENGTH = 2048
PUSH_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "android.googleapis.com",
        "jmt17.google.com",
        "updates.push.services.mozilla.com",
    }
)
#: Hosts allowed by suffix; the suffix must follow a label of its own
#: (`web.push.apple.com`, never `push.apple.com` itself).
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")
_B64U = re.compile(r"[A-Za-z0-9_-]*={0,2}")
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,32}")

# What a send says when it did not deliver: fixed French strings, never an
# exception's text.
DISABLED = "non envoyé : les envois sont désactivés sur ce serveur"
TIMEOUT_ERROR = "délai dépassé"
UNREACHABLE = "service de notification injoignable"
NETWORK_ERROR = "erreur réseau"
KEY_REFUSED = "clé refusée — réactivez les notifications sur cet appareil"
GONE = "appareil désinscrit par le service de notification"
INTERNAL_ERROR = "erreur interne"
ENDPOINT_INVALID = "Adresse de notification invalide."
ENDPOINT_UNKNOWN = "Ce service de notification n'est pas accepté."
KEYS_INVALID = "Clés de l'appareil invalides."


def refused(status: int) -> str:
    return f"refusé par le service (code {status})"


class BadEndpoint(ValueError):
    """An endpoint outside the allowlist; its message is French."""


class BadKeys(ValueError):
    """A subscription's p256dh or auth that is not a P-256 point and a
    16-byte secret; its message is French."""


@dataclass(frozen=True)
class PushResult:
    """What one send did. `status` is the push service's HTTP status (None
    when none answered); `gone` means the subscription no longer exists
    (404/410); `error` is a fixed French string, "" when `ok`."""

    ok: bool
    status: int | None = None
    gone: bool = False
    error: str = ""


def b64u_encode(data: bytes) -> str:
    """base64url without padding (JWT parts, keys)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """base64url, padded or not. ValueError on anything else - the standard
    decoder silently skips foreign characters."""
    if not isinstance(text, str) or not _B64U.fullmatch(text):
        raise ValueError("not base64url")
    text = text.rstrip("=")
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


def sending_enabled() -> bool:
    """A production server: an https SITE_URL, DEBUG off, a strong SECRET_KEY."""
    return (
        str(settings.SITE_URL or "").startswith("https://")
        and not settings.DEBUG
        and secret_key_problem(settings.SECRET_KEY) == ""
    )


_lock = threading.Lock()
#: sha256(SECRET_KEY) -> its VAPID private key.
_keys: dict[str, ec.EllipticCurvePrivateKey] = {}
#: (public key fingerprint, aud, sub) -> (token, issued at).
_tokens: dict[tuple[str, str, str], tuple[str, int]] = {}


def vapid_private_key() -> ec.EllipticCurvePrivateKey:
    """The server's VAPID key, derived from SECRET_KEY (see the docstring)."""
    secret = str(settings.SECRET_KEY or "").encode("utf-8")
    digest = hashlib.sha256(secret).hexdigest()
    with _lock:
        key = _keys.get(digest)
    if key is None:
        material = HKDF(algorithm=hashes.SHA256(), length=48, salt=VAPID_SALT, info=VAPID_INFO).derive(secret)
        scalar = int.from_bytes(material, "big") % (P256_ORDER - 1) + 1
        key = ec.derive_private_key(scalar, CURVE)
        with _lock:
            _keys[digest] = key
    return key


def _public_bytes(key) -> bytes:
    """The 65-byte uncompressed point of a private or public key."""
    public = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return public.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_public_key() -> str:
    """The browser's `applicationServerKey`, base64url."""
    return b64u_encode(_public_bytes(vapid_private_key()))


def check_endpoint(url) -> str:
    """`url` rebuilt from its checked parts - https, port 443, a host of the
    allowlist - or BadEndpoint."""
    if not isinstance(url, str) or not url or len(url) > MAX_ENDPOINT_LENGTH or not url.isascii():
        raise BadEndpoint(ENDPOINT_INVALID)
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise BadEndpoint(ENDPOINT_INVALID)
    # The scheme exactly as written: urlsplit lower-cases it.
    if not url.startswith("https://"):
        raise BadEndpoint(ENDPOINT_INVALID)
    parts = urlsplit(url)
    if any(char in parts.netloc for char in "\\@#%") or parts.username is not None:
        raise BadEndpoint(ENDPOINT_INVALID)
    try:
        port = parts.port
    except ValueError:
        raise BadEndpoint(ENDPOINT_INVALID) from None
    if port not in {None, 443}:
        raise BadEndpoint(ENDPOINT_INVALID)
    # hostname is lower-cased; a trailing dot or an IP literal fails the shape.
    host = parts.hostname or ""
    if not _HOST.fullmatch(host):
        raise BadEndpoint(ENDPOINT_INVALID)
    if host not in PUSH_HOSTS and not host.endswith(PUSH_HOST_SUFFIXES):
        raise BadEndpoint(ENDPOINT_UNKNOWN)
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{parts.path}{query}"


def check_keys(p256dh, auth) -> tuple[bytes, bytes]:
    """The subscription's keys decoded: a point on P-256 (65 bytes) and the
    16-byte auth secret - or BadKeys."""
    if not isinstance(p256dh, str) or not isinstance(auth, str) or len(p256dh) > 100 or len(auth) > 40:
        raise BadKeys(KEYS_INVALID)
    try:
        point = b64u_decode(p256dh)
        secret = b64u_decode(auth)
    except ValueError:
        raise BadKeys(KEYS_INVALID) from None
    if len(point) != 65 or len(secret) != 16:
        raise BadKeys(KEYS_INVALID)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(CURVE, point)
    except ValueError:
        raise BadKeys(KEYS_INVALID) from None
    return point, secret


def encrypt(
    plaintext: bytes, ua_public: bytes, auth_secret: bytes, *, salt=None, as_private=None, rs=RECORD_SIZE
) -> bytes:
    """RFC 8291 « aes128gcm », one record: the 86-byte header (salt, rs, the
    ephemeral public key) then the ciphertext. A new ephemeral key and salt
    per message unless given (the RFC's test vector gives them)."""
    if len(plaintext) > MAX_PLAINTEXT or len(plaintext) + 1 + 16 > rs:
        raise ValueError("payload too large for one record")
    salt = os.urandom(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("the salt is 16 bytes")
    as_private = ec.generate_private_key(CURVE) if as_private is None else as_private
    as_public = _public_bytes(as_private)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(ecdh_secret)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    header = salt + struct.pack("!IB", rs, len(as_public)) + as_public
    # 0x02: the last (and only) record's delimiter, no padding.
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def _origin(endpoint: str) -> str:
    """`scheme://host[:port]`, the JWT's `aud` (the default port left out)."""
    parts = urlsplit(endpoint)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    default = {"https": 443, "http": 80}.get(scheme)
    return f"{scheme}://{host}" if port in {None, default} else f"{scheme}://{host}:{port}"


def _compact(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign_jwt(claims: dict, key: ec.EllipticCurvePrivateKey) -> str:
    signing_input = f"{b64u_encode(_compact({'typ': 'JWT', 'alg': 'ES256'}))}.{b64u_encode(_compact(claims))}"
    r, s = decode_dss_signature(key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    # JWS ES256 is the raw r‖s, 64 bytes - not cryptography's DER.
    return f"{signing_input}.{b64u_encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def vapid_authorization(endpoint: str, *, now: int, private_key=None) -> str:
    """The `Authorization` header for `endpoint`: `vapid t=<jwt>, k=<key>`.
    A token is reused for an hour per (key, aud) - a change of SECRET_KEY
    gets its own."""
    key = private_key or vapid_private_key()
    public = b64u_encode(_public_bytes(key))
    aud = _origin(endpoint)
    sub = str(settings.SITE_URL or "")
    cache_key = (hashlib.sha256(public.encode("ascii")).hexdigest()[:16], aud, sub)
    with _lock:
        cached = _tokens.get(cache_key)
    if cached is not None and 0 <= now - cached[1] < JWT_REUSE:
        token = cached[0]
    else:
        claims: dict[str, str | int] = {"aud": aud, "exp": now + JWT_LIFETIME}
        if sub:
            claims["sub"] = sub
        token = _sign_jwt(claims, key)
        with _lock:
            for old in [k for k, (_, issued) in _tokens.items() if not 0 <= now - issued < JWT_REUSE]:
                del _tokens[old]
            _tokens[cache_key] = (token, now)
    return f"vapid t={token}, k={public}"


def _clip(text, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def _longest_fitting(text: str, fits) -> str:
    """The longest clip of `text` (with « … ») that `fits`, "" when none
    does: a binary search over its length, the bytes being what counts."""
    if fits(text):
        return text
    low, high = 0, len(text) - 1  # the clip of `low` characters fits ("" is assumed to)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(_clip(text, middle)):
            low = middle
        else:
            high = middle - 1
    return _clip(text, low) if low else ""


def payload_for(title, body, url: str, tag: str) -> bytes:
    """The notification as Declarative Web Push JSON (Safari 18.4+ shows it
    itself; our service worker reads `notification` for the others): title
    and body clipped with « … », the whole under PAYLOAD_MAX_BYTES - the
    body shortened first, then the title."""

    def encoded(title: str, body: str) -> bytes:
        notification = {"title": title, "body": body, "navigate": url, "tag": tag, "lang": "fr-FR", "dir": "ltr"}
        return _compact({"web_push": 8030, "notification": notification, "mutable": False})

    title = _clip(title, TITLE_MAX_LENGTH)
    body = _longest_fitting(_clip(body, BODY_MAX_LENGTH), lambda body: len(encoded(title, body)) <= PAYLOAD_MAX_BYTES)
    title = _longest_fitting(title, lambda title: len(encoded(title, body)) <= PAYLOAD_MAX_BYTES)
    data = encoded(title, body)
    if len(data) > PAYLOAD_MAX_BYTES:
        raise ValueError("the notification's link alone is too long")
    return data


def _where(endpoint) -> str:
    """A device as a log names it: its push service's host and the first 8
    hex of the endpoint's sha256 - never the endpoint, a bearer token."""
    text = str(endpoint or "")
    try:
        host = urlsplit(text).hostname or "?"
    except ValueError:
        host = "?"
    return f"{host} #{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:8]}"


_local = threading.local()


def _session() -> requests.Session:
    """This thread's session: no proxy or certificate setting from the
    environment."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _local.session = session
    return session


def _send_request(url: str, **kw) -> requests.Response:
    kw["allow_redirects"] = False
    kw.setdefault("timeout", TIMEOUT)
    return _session().post(url, **kw)


#: The one HTTP entry point, looked up at call time (see the docstring): the
#: test run replaces it.
_post = _send_request
#: The wait before a retry, patched by the tests.
_sleep = time.sleep


def _headers(url: str, *, ttl: int, topic: str, now: int) -> dict[str, str]:
    headers = {
        "TTL": str(max(1, int(ttl))),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Urgency": "high",
        "Authorization": vapid_authorization(url, now=now),
    }
    if topic:
        if not _TOPIC.fullmatch(topic):
            raise ValueError("a topic is 1 to 32 base64url characters")
        headers["Topic"] = topic
    return headers


def _answer(status: int) -> tuple[PushResult, bool]:
    """(result, worth a retry) for an HTTP status."""
    if 200 <= status < 300:
        return PushResult(ok=True, status=status), False
    if status in {404, 410}:
        return PushResult(ok=False, status=status, gone=True, error=GONE), False
    if status in {401, 403}:
        return PushResult(ok=False, status=status, error=KEY_REFUSED), False
    return PushResult(ok=False, status=status, error=refused(status)), status == 429 or status >= 500


def _attempt(post, url: str, body: bytes, headers: dict[str, str]) -> tuple[PushResult, bool]:
    try:
        response = post(url, data=body, headers=headers, timeout=TIMEOUT, allow_redirects=False)
    except requests.Timeout:
        return PushResult(ok=False, error=TIMEOUT_ERROR), True
    except requests.ConnectionError:
        return PushResult(ok=False, error=UNREACHABLE), True
    except (requests.RequestException, OSError, ValueError):
        return PushResult(ok=False, error=NETWORK_ERROR), True
    return _answer(int(response.status_code))


def send(endpoint, p256dh, auth, payload: bytes, *, ttl: int, topic="", retry=True, now=None, post=None) -> PushResult:
    """Push `payload` (payload_for's bytes) to one device. Never raises for
    anything the device, the network or the push service does: nothing is
    sent unless `sending_enabled()`, the endpoint is checked against the
    allowlist first, and a 429, a 5xx or a network error is retried once
    after RETRY_DELAY seconds (`retry=False`: never - the « essai » waits on
    a page). `now` (seconds since the epoch) dates the JWT; `post` replaces
    `_post`."""
    if not sending_enabled():
        return PushResult(ok=False, error=DISABLED)
    try:
        url = check_endpoint(endpoint)
        ua_public, auth_secret = check_keys(p256dh, auth)
    except (BadEndpoint, BadKeys) as exc:
        return PushResult(ok=False, error=str(exc))
    try:
        headers = _headers(url, ttl=ttl, topic=topic, now=int(time.time() if now is None else now))
        body = encrypt(payload, ua_public, auth_secret)
    except Exception:
        logger.exception("Notification non préparée pour %s", _where(endpoint))
        return PushResult(ok=False, error=INTERNAL_ERROR)
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        result, again = _attempt(post or _post, url, body, headers)
        if not again or attempt == attempts - 1:
            break
        _sleep(RETRY_DELAY)
    if not result.ok:
        logger.warning("Notification non envoyée à %s : %s", _where(url), result.error)
    return result
