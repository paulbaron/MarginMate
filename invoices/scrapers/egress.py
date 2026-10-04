"""Where another bar's mailbox may send the server: a public address only.

Every espace types its own IMAP server on « Identifiants », and the server
connects to it from the platform owner's home network - the router, the
other machines, the server itself (MarginMate listens on 127.0.0.1). A name
that leads there (« localhost », a « .local » name, a public name pointing at
127.0.0.1 or 192.168.x.x, an IPv4-mapped IPv6 address) would let a bar probe
that network's port 993. So, outside the platform owner's espace
(accounts.tenancy.server_accounts_allowed):

- the page refuses a local name when it is saved (`local_name`,
  accounts.credentials);
- the mailbox search resolves the server's name before IMAP4_SSL connects
  (`check_mail_host`) and refuses anything but public unicast addresses -
  loopback, private, link-local, shared (CGNAT), multicast, reserved and
  unspecified alike. The name is resolved again by the connection itself:
  a name changing its answer in between (DNS rebinding) reaches a host that
  must still pass the TLS check of a certificate for that name.

`resolve` is looked up at each call: a test patches it, and never resolves
a name for real (tests.support.NoNetworkTestCase refuses a lookup that is
not the machine's own).
"""

from __future__ import annotations

import ipaddress
import socket

#: The IMAP-over-TLS port, the only one the mailbox uses.
IMAP_SSL_PORT = 993
#: Names the internet does not resolve - a local network's, or the machine's.
LOCAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".lan",
    ".home",
    ".home.arpa",
    ".internal",
    ".intranet",
    ".corp",
)
LOCAL_NAME = "« {host} » désigne un réseau local : indiquez le serveur IMAP de votre messagerie, comme imap.gmail.com."
NOT_PUBLIC = (
    "Le serveur IMAP « {host} » mène au réseau local du serveur ou à une adresse réservée : la boîte mail "
    "n'est pas interrogée."
)
UNRESOLVED = "Le serveur IMAP « {host} » est introuvable : vérifiez son nom sur la page Identifiants."


class EgressRefused(RuntimeError):
    """A mailbox's server that is not on the internet. Carries a French
    sentence naming the host the bar typed, never an address it led to."""


def resolve(host: str, port: int):
    """The addresses `host` resolves to (socket.getaddrinfo's tuples)."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def local_name(host: str) -> bool:
    """Whether `host` is a name only a local network or the machine itself
    answers to: « localhost », a single label, a local suffix."""
    name = (host or "").strip().lower().rstrip(".")
    return not name or name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES)


def address_allowed(text: str) -> bool:
    """Whether a resolved address is a public unicast one (an IPv6 address
    wrapping an IPv4 one - IPv4-mapped, 6to4 - judged as its IPv4: Python
    calls 2002:c0a8:0101::1 global, and a 6to4 relay takes it to
    192.168.1.1)."""
    try:
        address = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    elif address.version == 6 and address.sixtofour is not None:
        address = address.sixtofour
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_private
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )


def check_mail_host(host: str, port: int = IMAP_SSL_PORT) -> None:
    """EgressRefused unless every address `host` resolves to is public."""
    if local_name(host):
        raise EgressRefused(LOCAL_NAME.format(host=host))
    try:
        found = resolve(host, port)
    except (OSError, UnicodeError):
        raise EgressRefused(UNRESOLVED.format(host=host)) from None
    addresses = {info[4][0] for info in found or ()}
    if not addresses or not all(address_allowed(address) for address in addresses):
        raise EgressRefused(NOT_PUBLIC.format(host=host))
