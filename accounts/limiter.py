"""A simple brake on guessing: the login pages (`/connexion/`, and the admin's
own) and the signup page count their attempts, and past a limit in `WINDOW`
they answer « réessayez plus tard » without checking anything.

What is counted, per action (`_counters`):

* **the login** (security audit ANON-4, DEPLOY-4; review of 29/09,
  LIMITER-LOCKOUT): the hard stop is ONE address from ONE place - `LIMIT`
  attempts per (place, e-mail). A stranger's failures from his places hold
  those places. Beside it, two much higher ceilings: one address from
  everywhere together (`EMAIL_LIMIT` - guesses spread over many places stop
  there too), and one place for every address together (`IP_LIMIT` - one
  place trying a few passwords on many addresses). It used to be `LIMIT`
  for the address alone and `LIMIT` for the place alone: ten wrong
  passwords from anywhere kept the owner out from everywhere, and behind a
  proxy - every visitor at the proxy's address - ten failures by anybody
  kept every bar out.

  The address's ceiling is one a stranger who knows the address CAN fill,
  from enough places (ten proxies; ten addresses of one IPv6 /64 before
  a /64 was one place): it then held the owner out of both doors, the PC
  included, for as long as the stranger went on sending. So it never holds
  back:

  - **a device the address logged in on** (« appareil connu »): every
    successful login - the login page, the signup's, the admin's once its
    page hands its answer to `remember_device` - leaves a signed cookie
    (`DEVICE_COOKIE`, HttpOnly, SameSite=Lax, Secure with the session
    cookie, `DEVICE_MAX_AGE`) naming the addresses logged in on that
    browser - as keyed hashes (`address_mark`), never written out: a shared
    device does not tell who used it. An attempt carrying a valid one for
    its address is judged on its place and on that device's own failures
    (`LIMIT`, from every place together: a cookie copied off the device is
    no pass for guesses spread over many places) - never on the ceiling. A
    logout keeps the cookie; a success renews it.
  - **the PC itself** (`directly_from_this_pc`): a browser on the machine
    running `manage.py serve` reaches Waitress as 127.0.0.1, host
    127.0.0.1 or localhost, with no proxy's mark. cloudflared connects from
    127.0.0.1 too, but Waitress makes its requests the visitor's
    (REMOTE_ADDR from X-Forwarded-For, the header kept), and Cloudflare
    marks every request it forwards (CF-Ray, CF-Connecting-IP, CDN-Loop) and
    names the public host: any one of those, and a request is not the PC.
    runserver answering the tunnel - it reads no X-Forwarded-For, every
    visitor is 127.0.0.1 - is refused the same way.

  Both still meet the hard stop of their place and the place's ceiling.
* **the signup**: `LIMIT` per place and `LIMIT` per e-mail. Its guesses are
  invitation codes, and the e-mail typed with them is anybody's choice: the
  place is what holds them back.

The place is REMOTE_ADDR (`client_place`), an IPv6 client's /64: Cloudflare
hands over the visitor's full address, and one /64 - a phone's network, a
rented server's allocation - is 2^64 addresses free to rotate in. Behind the
Cloudflare Tunnel, Waitress makes REMOTE_ADDR the visitor's address
(`manage.py serve`: cloudflared on 127.0.0.1 is the one trusted proxy, and
only the right-most X-Forwarded-For entry - the one Cloudflare wrote - is
taken); nothing here reads a header a request could set itself to be let in
- the headers `directly_from_this_pc` reads can only ever keep a request
OUT of the PC's exemption.

An attempt is counted BEFORE its password or its code is looked at
(`reserve`), and a success gives it back (`succeeded`). Checked first and
counted afterwards, guesses arriving at once - a threaded server - all
passed the check before the first of them was counted: as many guesses as
one could send together. A refused attempt counts nothing (its reservation
is given back): guesses from a place already held back never add to the
count of the address they name.

The counts live in Django's cache - the default one, in memory: right for
`manage.py serve`, ONE process (Waitress, several threads) whose memory
every request shares; several processes would each count their own. They
rely on the cache's `add` (the window starts at the first attempt and is
not extended by the next ones) and on an `incr` that is ATOMIC and KEEPS
the key's expiry (Redis or Memcached for several processes). The file and
database caches do neither - they re-set the key with their default
TIMEOUT at every `incr`, so ten failures held a login back 5 minutes from
the latest instead of 15 from the first - and the dummy cache keeps
nothing: the system check accounts.W002 warns about all three.

A success forgets the counts it was judged on and gives the client its
attempt back, never more: otherwise one account of one's own would reset
the count between guesses at somebody else's. A known device's success
therefore leaves the address's ceiling as the strangers filled it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.crypto import salted_hmac

from .users import normalize_email

#: The hard stop: one address from one place, one device (the login); one
#: place, and one address (the signup).
LIMIT = 10
#: The login: one address, from every place together - but a known device
#: and the PC itself.
EMAIL_LIMIT = 100
#: The login: one place, every address together.
IP_LIMIT = 50
WINDOW_SECONDS = 15 * 60

LOGIN = "connexion"
SIGNUP = "inscription"

TOO_MANY = "Trop de tentatives sans succès depuis cette adresse ou pour ce compte : réessayez dans un quart d'heure."

#: An IPv6 client is its /64.
IPV6_PLACE_PREFIX = 64

#: The « appareil connu » cookie: the addresses that logged in on a browser.
DEVICE_COOKIE = "marginmate_appareil"
DEVICE_SALT = "marginmate.limiter.appareil-connu"
DEVICE_MAX_AGE = 180 * 24 * 60 * 60
#: How many addresses one device remembers, the last logged in first (a
#: shared PC: two bars, the platform's owner).
DEVICE_ADDRESSES = 5

#: A proxy's mark: a request carrying one came through a proxy, whatever its
#: address says (`directly_from_this_pc`). Every header whose name starts
#: with CF- counts too (Cloudflare's own).
FORWARDING_HEADERS = frozenset(
    {
        "HTTP_X_FORWARDED_FOR",
        "HTTP_X_FORWARDED_PROTO",
        "HTTP_X_FORWARDED_HOST",
        "HTTP_X_FORWARDED_PORT",
        "HTTP_X_FORWARDED_BY",
        "HTTP_FORWARDED",
        "HTTP_X_REAL_IP",
        "HTTP_TRUE_CLIENT_IP",
        "HTTP_CDN_LOOP",
        "HTTP_VIA",
    }
)
#: The names a browser on the PC reaches the server by.
PC_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def client_ip(request) -> str:
    return str(request.META.get("REMOTE_ADDR") or "")


def client_place(request) -> str:
    """The place the counters hold back: REMOTE_ADDR, an IPv6 client's /64
    (« 2001:db8:1:1::/64 »). An IPv4 client written as IPv6
    (« ::ffff:203.0.113.7 », what a server on [::] reports) is its IPv4
    address - its /64 would be ::/64, every IPv4 client at once. Anything
    that is no address stays as it is."""
    ip = client_ip(request)
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 4:
        return ip
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(ipaddress.IPv6Network((address.packed, IPV6_PLACE_PREFIX), strict=False))


def _host_name(host: str) -> str:
    """« 127.0.0.1:8765 » → « 127.0.0.1 », « [::1]:8765 » → « ::1 »."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def directly_from_this_pc(request) -> bool:
    """A browser on the machine running the server, reaching it directly:
    a loopback REMOTE_ADDR, a Host naming the PC, and no proxy's mark at all.
    A tunnelled request is never one: Waitress makes it the visitor's
    address, keeps X-Forwarded-For, and Cloudflare adds its CF- headers and
    the public host - any single one of them is enough to say no."""
    try:
        address = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_loopback:
        return False
    meta = request.META
    if any(name in meta for name in FORWARDING_HEADERS) or any(name.startswith("HTTP_CF_") for name in meta):
        return False
    return _host_name(str(meta.get("HTTP_HOST") or "")) in PC_HOSTS


def address_mark(email) -> str:
    """What the device cookie keeps of an address: a hash keyed by the
    SECRET_KEY, never the address."""
    return salted_hmac(DEVICE_SALT, normalize_email(email), algorithm="sha256").hexdigest()[:32]


def _device(request) -> dict | None:
    """The request's « appareil connu » cookie, checked (signature, age,
    shape), or None."""
    raw = request.COOKIES.get(DEVICE_COOKIE) if hasattr(request, "COOKIES") else None
    if not raw:
        return None
    try:
        value = signing.loads(raw, salt=DEVICE_SALT, max_age=DEVICE_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    device, marks = value.get("n"), value.get("e")
    if not isinstance(device, str) or not device or not isinstance(marks, list):
        return None
    if not all(isinstance(mark, str) for mark in marks):
        return None
    return {"n": device, "e": marks}


def known_device(request, email) -> str:
    """The device's id when the request carries a valid « appareil connu »
    cookie for `email`, else ""."""
    email = normalize_email(email)
    device = _device(request)
    if not email or device is None or address_mark(email) not in device["e"]:
        return ""
    return device["n"]


def device_cookie(emails, device: str = "") -> str:
    """The cookie's value: `emails` (the last logged in first, at most
    `DEVICE_ADDRESSES`) for the device `device` (a new one if blank)."""
    marks = []
    for email in emails:
        mark = address_mark(email)
        if mark not in marks:
            marks.append(mark)
    return _signed(device or secrets.token_hex(8), marks)


def _signed(device: str, marks: list[str]) -> str:
    return signing.dumps({"n": device, "e": marks[:DEVICE_ADDRESSES]}, salt=DEVICE_SALT, compress=False)


#: Set on the request by `succeeded` for a login: the address to remember.
_LOGGED_IN_AS = "_marginmate_logged_in_as"


def remember_device(request, response, email=None):
    """`response` with the « appareil connu » cookie for `email` - by
    default the address whose login `succeeded` on this request - put first
    among the addresses the device already knows, its 180 days counted
    again. Nothing when nobody logged in."""
    email = normalize_email(email if email is not None else getattr(request, _LOGGED_IN_AS, ""))
    if not email:
        return response
    device = _device(request) or {"n": secrets.token_hex(8), "e": []}
    mark = address_mark(email)
    value = _signed(device["n"], [mark, *(known for known in device["e"] if known != mark)])
    response.set_cookie(
        DEVICE_COOKIE,
        value,
        max_age=DEVICE_MAX_AGE,
        path="/",
        secure=bool(settings.SESSION_COOKIE_SECURE),
        httponly=True,
        samesite="Lax",
    )
    return response


def _key(action: str, kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]
    return f"marginmate:echecs:{action}:{kind}:{digest}"


#: The client's own counter: a success gives its attempt back, never more.
PLACE = "ip"
#: A known device's own counter (the login).
DEVICE = "appareil"


def _counters(action: str, request, email) -> list[tuple[str, str, int]]:
    """(kind, cache key, its limit) of every counter this attempt is judged
    on. The limits are read here, at each attempt (a test lowers them)."""
    place = client_place(request)
    email = normalize_email(email)
    if action == LOGIN:
        wanted = [("pair", f"{place}\n{email}" if email else "", LIMIT)]
        device = known_device(request, email)
        if device:
            wanted.append((DEVICE, device, LIMIT))
        elif not directly_from_this_pc(request):
            wanted.append(("email", email, EMAIL_LIMIT))
        wanted.append((PLACE, place, IP_LIMIT))
    else:
        wanted = [(PLACE, place, LIMIT), ("email", email, LIMIT)]
    return [(kind, _key(action, kind, value), limit) for kind, value, limit in wanted if value]


def _give_back(key: str) -> None:
    try:
        cache.decr(key)
    except ValueError:
        # Gone meanwhile (expired, or forgotten by a success): nothing to give back.
        pass


def blocked(action: str, request, email) -> bool:
    """Past a limit for this attempt (reading only)."""
    counters = _counters(action, request, email)
    counts = cache.get_many([key for _, key, _ in counters])
    return any(counts.get(key, 0) >= limit for _, key, limit in counters)


def reserve(action: str, request, email) -> bool:
    """Count one attempt on each of its counters, BEFORE anything is
    checked. False - and nothing counted - when any is past its limit: the
    attempt must then check nothing."""
    counters = _counters(action, request, email)
    over = False
    for _, key, limit in counters:
        # add() starts the window only if there is none; incr() keeps it.
        cache.add(key, 0, WINDOW_SECONDS)
        try:
            count = cache.incr(key)
        except ValueError:
            # Expired between the two calls: this attempt starts a new window.
            cache.set(key, 1, WINDOW_SECONDS)
            count = 1
        over = over or count > limit
    if not over:
        return True
    for _, key, _ in counters:
        _give_back(key)
    return False


def succeeded(action: str, request, email) -> None:
    """A success: the counts it was judged on are forgotten, and the client
    gets this attempt back (never its failures). A login's address is kept
    on the request for `remember_device`."""
    for kind, key, _ in _counters(action, request, email):
        if kind == PLACE:
            _give_back(key)
        else:
            cache.delete(key)
    if action == LOGIN:
        setattr(request, _LOGGED_IN_AS, normalize_email(email))
