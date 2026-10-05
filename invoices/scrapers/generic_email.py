"""Generic, pattern-driven IMAP invoice fetcher - replaces having to hand-write
a dedicated scraper module (like the old uba_email.py) for every new email
based invoice source. Driven by an EmailInvoiceSource's regex patterns
instead of hardcoded search terms.

IMAP's own SEARCH command only supports crude substring matching on FROM/
SUBJECT (no regex, no body search worth relying on), so this can't just
build one clever SEARCH string the way the old UBA-specific scraper did.
Instead it does a two-phase fetch: a broad IMAP SEARCH scoped only by date
range, then a header-only fetch to test sender_pattern/subject_pattern in
Python before doing anything expensive, and only for messages that pass
that does it fetch the full message to test body_pattern and pull
attachments.

Both fetch phases pull many messages per IMAP round trip (see BATCH_SIZE)
rather than one FETCH command per message - a wide date range easily
covers thousands of emails, and a network round trip per message for each
of the two phases made a several-month scan take minutes. Batching cuts
that to a handful of round trips per phase; see _parse_batched_fetch for
how a single multi-message FETCH response gets split back apart.

Uses BODY.PEEK[...] throughout, not RFC822 - RFC822 marks a message read as
a side effect, which would be actively annoying for the "test this pattern"
dry-run feature that's meant to be safely re-run against the real mailbox
while iterating on a pattern.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import functools
import imaplib
import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..models import INVOICE_ATTACHMENT_PATTERN

DEFAULT_IMAP_HOST = "imap.gmail.com"
MAILBOX_MISSING = (
    "L'adresse ou le mot de passe d'application de la boîte mail manque : renseignez-les sur la page Identifiants."
)
MAILBOX_UNBOUND = (
    "Le mot de passe d'application de la boîte mail n'est rattaché à aucun serveur : ressaisissez-le sur la page "
    "Identifiants."
)


def mailbox_credentials() -> tuple[str, str, str]:
    """(address, app password, IMAP server) - the server is the one the
    password was typed for. A password typed on « Identifiants » goes to the
    server recorded with it (accounts/vault.py bindings), whatever the page's
    host field says now; one from the .env goes to the .env's server, never
    to a host typed on the page: changing the server on the page must not
    send the .env's password elsewhere. The .env's are read in the owner's
    tenant only (`accounts.tenancy.server_accounts_allowed`)."""
    from accounts import vault

    state = vault.load()
    stored = state.values
    address = stored.get("INVOICE_EMAIL_ADDRESS") or vault.server_setting("INVOICE_EMAIL_ADDRESS")
    if stored.get("INVOICE_EMAIL_APP_PASSWORD"):
        app_password = stored["INVOICE_EMAIL_APP_PASSWORD"]
        host = state.bindings.get("INVOICE_EMAIL_APP_PASSWORD", "")
        if not host:
            raise RuntimeError(MAILBOX_UNBOUND)
    else:
        # The server's mailbox, in the owner's tenant only: another bar's
        # gather reads its own page's values or nothing (vault.server_setting).
        app_password = vault.server_setting("INVOICE_EMAIL_APP_PASSWORD")
        host = vault.server_setting("INVOICE_IMAP_HOST") or DEFAULT_IMAP_HOST
    if not address or not app_password:
        raise RuntimeError(MAILBOX_MISSING)
    return address, app_password, host


#: The biggest message (an IMAP « literal ») the server takes into memory:
#: past it, a mail server - one a bar names on its « Identifiants » - could
#: announce gigabytes and the one process every bar runs in would try to
#: hold them. Generous: a mail provider's own limit is 25 to 35 MB, encoded.
MAX_LITERAL_BYTES = 50 * 1024 * 1024
MESSAGE_TOO_BIG = "La boîte mail annonce un message de plus de 50 Mo : la recherche s'arrête là, rien de plus n'est lu."
#: Outside the platform owner's espace, what the answer to one command may
#: bring in, its lines and its literals together: imaplib keeps a whole
#: FETCH answer in memory, and 150 messages of 49 MB each was 7 GB. Room for
#: one message at the cap and what surrounds it (MAX_BATCH_BYTES).
MAX_RESPONSE_BYTES = 100 * 1024 * 1024
RESPONSE_TOO_BIG = "La boîte mail a envoyé plus de 100 Mo en une seule réponse : la recherche s'arrête là."
#: Outside the platform owner's espace, what one search may read in all: the
#: attachments it keeps stay in memory until it ends, and so does whatever
#: else the server sends that imaplib keeps.
MAX_SEARCH_BYTES = 500 * 1024 * 1024
SEARCH_TOO_BIG = "La recherche a déjà lu 500 Mo de la boîte mail : choisissez une période plus courte."
#: Outside the platform owner's espace, phase 2 fetches its messages by the
#: sizes phase 1 read (RFC822.SIZE): this much per FETCH at most - a message
#: bigger than that alone.
MAX_BATCH_BYTES = 40 * 1024 * 1024
TOO_BIG_SKIPPED = "Passé : un e-mail de plus de 50 Mo (« {subject} »)."


class MessageTooBig(imaplib.IMAP4.abort):
    """More than the server takes into memory, announced or sent by the mail
    server (MAX_LITERAL_BYTES; outside the owner's espace MAX_RESPONSE_BYTES
    and MAX_SEARCH_BYTES too): the connection is given up, as imaplib gives
    up a broken one."""


class _CappedLiterals:
    """In front of imaplib's IMAP4_SSL: `read(size)` is how imaplib takes a
    literal the server announced (imaplib._get_response) - refused past
    MAX_LITERAL_BYTES, in every espace."""

    def read(self, size):
        if size > MAX_LITERAL_BYTES:
            raise MessageTooBig(MESSAGE_TOO_BIG)
        return super().read(size)


class _Budgeted:
    """Behind the cap, outside the platform owner's espace: every byte the
    server sends - a literal (`read`), a line (`readline`: each is at most
    imaplib's _MAXLINE, but nothing limits how many) - counts against
    MAX_RESPONSE_BYTES for the answer to one command (counted again from
    each `send`) and MAX_SEARCH_BYTES for the connection. A literal is
    counted before it is read: imaplib's file allocates it whole."""

    _answer_bytes = 0
    _search_bytes = 0

    def send(self, data):
        self._answer_bytes = 0
        return super().send(data)

    def _count(self, size: int) -> None:
        self._answer_bytes += size
        self._search_bytes += size
        if self._answer_bytes > MAX_RESPONSE_BYTES:
            raise MessageTooBig(RESPONSE_TOO_BIG)
        if self._search_bytes > MAX_SEARCH_BYTES:
            raise MessageTooBig(SEARCH_TOO_BIG)

    def read(self, size):
        self._count(size)
        return super().read(size)

    def readline(self):
        line = super().readline()
        self._count(len(line))
        return line


@functools.lru_cache(maxsize=8)
def _capped(base: type, budgeted: bool = False) -> type:
    if budgeted:
        return type("BudgetedIMAP4_SSL", (_CappedLiterals, _Budgeted, base), {})
    return type("CappedIMAP4_SSL", (_CappedLiterals, base), {})


def _open_mailbox(host: str, *, budgeted: bool = False):
    """The IMAP connection, with a verifying TLS context and the literal cap
    - and, `budgeted` (another bar's mailbox), the byte budgets.
    imaplib.IMAP4_SSL is looked up now: a test's stand-in (a mock, a class
    that refuses) is called as it is."""
    opener = imaplib.IMAP4_SSL
    if isinstance(opener, type):
        opener = _capped(opener, budgeted)
    # A verifying context: imaplib's default (ssl._create_stdlib_context)
    # checks neither the certificate nor the host name, and the app password
    # went to whoever answered the TLS handshake (security review 01/10/2026).
    return opener(host, ssl_context=ssl.create_default_context(), timeout=FETCH_TIMEOUT_SECONDS)


FETCH_TIMEOUT_SECONDS = 45  # per IMAP operation - independent of how many emails there are in total
#: Phase 1's FETCH: the headers the patterns read - and, outside the
#: platform owner's espace, each message's size, which phase 2 batches by.
HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
SIZED_HEADER_QUERY = "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
BATCH_SIZE = 150  # messages per FETCH round trip - comfortably under IMAP servers' command-length limits
LOG_EVERY = 500  # scanned messages between liveness log lines, so a big date range doesn't look frozen


#: Said when the server answers the date range's SEARCH with anything but OK:
#: a refusal is no empty range.
SEARCH_REFUSED = "Recherche refusée par le serveur mail ({status})."


class SearchRefused(RuntimeError):
    """The date range's SEARCH answered with anything but OK (SEARCH_REFUSED,
    the status IMAP's own word): the app's sentence, which every espace's
    line says as it is (`failure_said`)."""


class IncompleteSearch(RuntimeError):
    """The search is not a whole one: the server left `unread` mails of the
    range unread (a FETCH answered NO, or timed out), a pattern was too slow
    on `slow` mails (a timeout is no « no match »), or the disk refused
    `unwritten` attachments (scrape_email_invoices). What was read -
    `matches` - stands. The gather imports it and records no coverage
    (invoices/coverage.py): the range is searched again next time.
    `downloaded`: scrape_email_invoices' files for `matches`."""

    def __init__(self, unread: int, matches: list, *, slow: int = 0, unwritten: int = 0):
        super().__init__()
        self.unread = unread
        self.slow = slow
        self.unwritten = unwritten
        self.matches = matches
        self.downloaded: list[tuple[str, date | None]] = []

    def __str__(self) -> str:
        reasons = []
        if self.unread:
            reasons.append(f"{self.unread} e-mail(s) non lu(s) par le serveur mail")
        if self.slow:
            reasons.append(f"motif trop lent sur {self.slow} e-mail(s)")
        if self.unwritten:
            reasons.append(f"{self.unwritten} pièce(s) jointe(s) non enregistrée(s) sur le disque")
        return f"Recherche incomplète : {', '.join(reasons)}."


@dataclass
class EmailAttachment:
    filename: str
    content: bytes


@dataclass
class EmailMatch:
    message_id: bytes
    sender: str
    subject: str
    email_date: date | None
    attachments: list[EmailAttachment] = field(default_factory=list)


def _format_date_for_imap(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _parse_email_date(raw_date: str | None) -> date | None:
    if not raw_date:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw_date)
        return parsed.date() if parsed else None
    except (TypeError, ValueError):
        return None


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for value, encoding in parts:
        if isinstance(value, bytes):
            try:
                decoded.append(value.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                decoded.append(value.decode("utf-8", errors="replace"))
        else:
            decoded.append(value)
    return "".join(decoded)


def _decode_part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _extract_text_body(msg) -> str:
    """Best-effort plain text of an email for body_pattern matching - prefers
    a real text/plain part, falls back to a crude tag-stripped text/html
    part (no HTML-parsing dependency needed for this)."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain_parts, html_parts = [], []
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_decode_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_decode_part_text(part))
    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
    return ""


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


_FETCH_SEQ_REGEX = re.compile(rb"^(\d+) ")


def _parse_batched_fetch(msg_data) -> dict[bytes, bytes]:
    """imaplib's fetch() response for a multi-message FETCH command is a
    flat list of (info, content) tuples (one pair per message) interleaved
    with bare b')' closing markers, in no guaranteed order - this pulls out
    {sequence_number: content_bytes} by reading the leading sequence number
    off each tuple's own info string, the standard way to correlate a
    batched IMAP response back to which message each part belongs to."""
    result: dict[bytes, bytes] = {}
    for part in msg_data:
        if not isinstance(part, tuple):
            continue
        info, content = part
        match = _FETCH_SEQ_REGEX.match(info)
        if match:
            result[match.group(1)] = content
    return result


_SIZE_REGEX = re.compile(rb"RFC822\.SIZE (\d+)")


def _parse_sizes(msg_data) -> dict[bytes, int]:
    """{sequence_number: RFC822.SIZE} from a FETCH answer that asked for it.
    The server says it before or after the header's literal, as it likes:
    after, it is in the bare bytes that follow that message's tuple (its
    trailer), which belong to the message the last tuple named."""
    sizes: dict[bytes, int] = {}
    current = None
    for part in msg_data:
        text = part[0] if isinstance(part, tuple) else part
        if not isinstance(text, bytes):
            continue
        start = _FETCH_SEQ_REGEX.match(text)
        if start:
            current = start.group(1)
        size = _SIZE_REGEX.search(text)
        if size and current is not None:
            sizes[current] = int(size.group(1))
    return sizes


def _batches_by_size(mail_ids: list, sizes: dict) -> list[list]:
    """Phase 2's batches outside the platform owner's espace: BATCH_SIZE
    messages at most, and MAX_BATCH_BYTES of announced sizes at most - a
    message bigger than that alone. A size the server did not say counts as
    MAX_LITERAL_BYTES: that message is fetched alone."""
    batches: list[list] = []
    batch: list = []
    weight = 0
    for mail_id in mail_ids:
        size = sizes.get(mail_id, MAX_LITERAL_BYTES)
        if batch and (len(batch) >= BATCH_SIZE or weight + size > MAX_BATCH_BYTES):
            batches.append(batch)
            batch, weight = [], 0
        batch.append(mail_id)
        weight += size
    if batch:
        batches.append(batch)
    return batches


# -- What a hosted espace's job log says of a search that failed -------------------------------------------------

LOGIN_REFUSED = (
    "identifiants ou commande refusés par le serveur : vérifiez l'adresse et le mot de passe d'application sur la "
    "page Identifiants."
)
CONNECTION_CUT = "connexion coupée par le serveur : réessayez plus tard."
CERTIFICATE_REFUSED = "certificat du serveur non valable : vérifiez le serveur IMAP sur la page Identifiants."
TLS_FAILED = "connexion chiffrée impossible avec ce serveur : vérifiez le serveur IMAP sur la page Identifiants."
NO_ANSWER = "le serveur ne répond pas : réessayez plus tard."
CONNECTION_REFUSED = "le serveur refuse la connexion : vérifiez le serveur IMAP sur la page Identifiants."
SERVER_NOT_FOUND = "serveur introuvable : vérifiez le serveur IMAP sur la page Identifiants."
NETWORK_FAILED = "connexion impossible : réessayez plus tard."
#: The app's own refusals, French already: said as they are.
OWN_SENTENCES = (MAILBOX_MISSING, MAILBOX_UNBOUND)


def failure_said(exc: BaseException, *, log=None) -> str:
    """What a job's log and the gather card say of a mailbox search that
    failed. In the platform owner's espace (and unbound) as always - the
    exception's own words. In any other the library's words - English, the
    server's host, a certificate's details - never reach the page (security
    audit LB-3): the app's own refusals as they are (a pattern refused, a
    server that is not public, a message too big, a mailbox missing, a
    search the server refused or left incomplete), a library's error as a
    fixed sentence by kind, anything else
    `common.SERVER_ERROR`, its detail to `log` (a logger)."""
    import socket

    import common
    from accounts.tenancy import current_tenant, server_accounts_allowed
    from invoices import integrations
    from returnables.patterns import PatternError

    from .egress import EgressRefused

    if current_tenant() is None or server_accounts_allowed():
        return str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, (PatternError, EgressRefused, MessageTooBig, SearchRefused, IncompleteSearch)):
        return str(exc).strip()
    if type(exc) is RuntimeError and str(exc) in (*OWN_SENTENCES, integrations.MAILBOX):
        return str(exc)
    for kinds, sentence in (
        (imaplib.IMAP4.abort, CONNECTION_CUT),
        (imaplib.IMAP4.error, LOGIN_REFUSED),
        (ssl.SSLCertVerificationError, CERTIFICATE_REFUSED),
        (ssl.SSLError, TLS_FAILED),
        (TimeoutError, NO_ANSWER),
        (ConnectionRefusedError, CONNECTION_REFUSED),
        (socket.gaierror, SERVER_NOT_FOUND),
        (OSError, NETWORK_FAILED),
    ):
        if isinstance(exc, kinds):
            return sentence
    return common.error_for_page(exc, log=log, what="Recherche dans la boîte mail")


def _extract_attachments(msg, attachment_regex: re.Pattern) -> list[EmailAttachment]:
    if not msg.is_multipart():
        return []
    attachments = []
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header_value(filename)
        if not attachment_regex.search(filename):
            continue
        content = part.get_payload(decode=True)
        if content is None:
            continue
        attachments.append(EmailAttachment(filename=filename, content=content))
    return attachments


def _invoice_matcher(pattern: str, field: str, log, *, hosted: bool):
    """One of an invoice source's patterns through
    returnables.patterns.invoice_mail_matcher, in every espace (security
    audit 04/10/2026), named in a refusal as its field is named on the
    source's form (EmailInvoiceSource.PATTERN_LABELS) - « Motif de mail »
    left the bar guessing which of four failed. In an espace that is not the
    platform owner's (`hosted`), a pattern finding something in an empty
    text is refused with its field's own sentence (EMPTY_MATCH_REASONS), and
    one too slow on MAX_MAIL_TIMEOUTS mails of the search stops it."""
    from returnables.patterns import MAX_MAIL_TIMEOUTS, invoice_mail_matcher

    from ..models import EmailInvoiceSource

    return invoice_mail_matcher(
        pattern,
        field_label=EmailInvoiceSource.PATTERN_LABELS[field],
        log=log,
        empty_reason=EmailInvoiceSource.EMPTY_MATCH_REASONS[field] if hosted else None,
        max_timeouts=MAX_MAIL_TIMEOUTS if hosted else None,
    )


def find_matching_emails(
    start_date: date,
    end_date: date,
    sender_pattern: str,
    subject_pattern: str = "",
    body_pattern: str = "",
    attachment_pattern: str = INVOICE_ATTACHMENT_PATTERN,
    log=print,
    on_progress=None,
    should_cancel=None,
    compile=None,
) -> list[EmailMatch]:
    """Searches the espace's invoice mailbox for emails matching every given
    pattern (blank subject/body pattern = match anything), fetching each
    matched attachment's bytes into memory (nothing written to disk here -
    see scrape_email_invoices for that). `on_progress(matched, total)` fires
    after each batch in both phases; `total` is how many messages fell in
    the date range, not how many will ultimately match - and the count
    reported during phase 1 (sender/subject only) can still shrink a bit by
    the time phase 2 (body_pattern too) finishes.

    `should_cancel` (a no-arg callable returning bool), when given, is
    checked between batches in both phases - a scan over a wide date range
    can take a while (thousands of emails), so this is what lets a "Cancel"
    button actually take effect promptly instead of only between whole
    invoice types. Cancelling mid-scan simply stops early and returns
    whatever was already found - nothing already matched is discarded.

    What the server does not hand over is never taken for nothing: a SEARCH
    answered anything but OK raises (SEARCH_REFUSED), and a batch whose FETCH
    is answered NO or times out is skipped, the rest read, and the search
    then raises IncompleteSearch carrying what it read - a gather recorded
    such a search as whole, and the skipped mails were never searched again.
    So is a mail a pattern was too slow on (a matcher counting its
    timeouts, `timed_out`): left out of this search, it is not a mail that
    does not match - an attachment name too slow drops that attachment, the
    mail's others still come.

    The mailbox is the bound espace's own (its « Identifiants »): refused
    unbound, before the settings are read or anything signs in
    (invoices/integrations.py) - scrape_email_invoices goes through here too.
    Outside the platform owner's espace its server must be a public address
    (scrapers.egress.check_mail_host), checked before anything connects.

    `compile` turns each pattern (sender, subject, body, attachment) into
    something with `.search(text)`. By default (an invoice source's patterns,
    and « Tester ») returnables.patterns.invoice_mail_matcher, in every
    espace: `re`'s meaning as before, but checked by the motif guard and
    matched with a timeout - a bare `re.compile` let one email's body hang
    the whole server on a backtracking pattern (security audit 04/10/2026);
    outside the platform owner's espace also refused when it finds something
    in an empty text, and stopping its source after MAX_MAIL_TIMEOUTS
    timeouts (_invoice_matcher). The returnables gather passes
    returnables.patterns.mail_matcher: a format's patterns are checked before
    anything compiles them, matched case-insensitively and with a timeout -
    a header anybody on the internet can write must not hang the gather. A
    pattern it refuses raises here, before anything signs in - by default
    naming its field (EmailInvoiceSource.PATTERN_LABELS).
    """
    from accounts.tenancy import integrations_allowed, server_accounts_allowed
    from invoices import integrations

    if not integrations_allowed():
        raise RuntimeError(integrations.MAILBOX)
    server = server_accounts_allowed()
    address, app_password, host = mailbox_credentials()

    def compiled(pattern: str, field: str):
        if compile is not None:
            return compile(pattern)
        return _invoice_matcher(pattern, field, log, hosted=not server)

    sender_regex = compiled(sender_pattern, "sender_pattern")
    subject_regex = compiled(subject_pattern, "subject_pattern") if subject_pattern else None
    body_regex = compiled(body_pattern, "body_pattern") if body_pattern else None
    attachment_regex = compiled(attachment_pattern or INVOICE_ATTACHMENT_PATTERN, "attachment_pattern")
    matchers = [regex for regex in (sender_regex, subject_regex, body_regex, attachment_regex) if regex is not None]

    def timeouts() -> int:
        # A bare compiled pattern (a test's `re.compile`) never times out.
        return sum(getattr(regex, "timed_out", 0) for regex in matchers)

    matches: list[EmailMatch] = []
    # The mails a pattern was too slow on, in either phase - once each.
    slow: set[bytes] = set()

    if not server:
        from . import egress

        egress.check_mail_host(host)
    # Another bar's server is held to the byte budgets, and phase 2 fetches
    # its messages by the sizes phase 1 read (_batches_by_size); the owner's
    # search is the one it always was.
    imap = _open_mailbox(host, budgeted=not server)
    said = (lambda exc: exc) if server else failure_said
    header_query = HEADER_QUERY if server else SIZED_HEADER_QUERY
    sizes: dict[bytes, int] = {}
    imap.login(address, app_password)
    try:
        imap.select("inbox")
        # BEFORE is exclusive (RFC 3501): the day after, or the end date's
        # emails - today's, by default - are left out.
        before = _format_date_for_imap(end_date + timedelta(days=1))
        search_criteria = f'SINCE "{_format_date_for_imap(start_date)}" BEFORE "{before}"'
        status, messages = imap.search(None, search_criteria)
        if status != "OK":
            # A refusal (« NO [UNAVAILABLE] ») is no empty range: taken for
            # one, the gather recorded the range searched.
            raise SearchRefused(SEARCH_REFUSED.format(status=status))
        if not messages or not messages[0]:
            log("Aucun e-mail sur cette période.")
            return matches

        mail_ids = messages[0].split()
        total = len(mail_ids)
        log(f"{total} e-mail(s) à examiner du {start_date:%d/%m/%Y} au {end_date:%d/%m/%Y}.")
        if on_progress:
            on_progress(0, total)

        # Phase 1: batch-fetch headers only, test sender_pattern/subject_pattern.
        # A batch the server does not hand over (a FETCH answered NO, or one
        # timing out) is counted unread and the rest read all the same: the
        # search then says it is incomplete (IncompleteSearch), never whole.
        header_matches: list[bytes] = []
        scanned = 0
        unread = 0
        for batch in _chunked(mail_ids, BATCH_SIZE):
            if should_cancel and should_cancel():
                log(f"Recherche annulée après {scanned}/{total} email(s) analysé(s).")
                return matches
            try:
                status, header_data = imap.fetch(b",".join(batch), header_query)
            except OSError as exc:
                log(f"{len(batch)} e-mail(s) passé(s) : erreur en lisant leurs en-têtes ({said(exc)}).")
                scanned += len(batch)
                unread += len(batch)
                continue
            if status != "OK":
                log(
                    f"{len(batch)} e-mail(s) passé(s) : le serveur mail a répondu {status} à la lecture de leurs en-têtes."
                )
                scanned += len(batch)
                unread += len(batch)
                continue
            headers_by_id = _parse_batched_fetch(header_data)
            if not server:
                sizes.update(_parse_sizes(header_data))
            for mail_id in batch:
                scanned += 1
                header_bytes = headers_by_id.get(mail_id)
                if header_bytes is None:
                    continue
                header_msg = email.message_from_bytes(header_bytes)
                sender = _decode_header_value(header_msg.get("From"))
                subject = _decode_header_value(header_msg.get("Subject"))
                timed_out_before = timeouts()
                matched = sender_regex.search(sender) and (not subject_regex or subject_regex.search(subject))
                if timeouts() > timed_out_before:
                    slow.add(mail_id)
                    continue
                if not matched:
                    continue
                if sizes.get(mail_id, 0) > MAX_LITERAL_BYTES:
                    # Announced past the cap: passed over on its own line,
                    # never fetched - the search goes on.
                    log(TOO_BIG_SKIPPED.format(subject=subject))
                    continue
                header_matches.append(mail_id)
            if on_progress:
                on_progress(len(header_matches), total)
            if scanned % LOG_EVERY < BATCH_SIZE:
                log(
                    f"{scanned}/{total} e-mail(s) examiné(s), {len(header_matches)} retenu(s) par l'expéditeur et "
                    "l'objet jusqu'ici."
                )

        log(f"{len(header_matches)} e-mail(s) retenu(s) par l'expéditeur et l'objet : lecture de leur contenu.")

        # Phase 2: batch-fetch the full message only for header matches, test
        # body_pattern and pull attachments.
        batches = _chunked(header_matches, BATCH_SIZE) if server else _batches_by_size(header_matches, sizes)
        for batch in batches:
            if should_cancel and should_cancel():
                log(f"Recherche annulée après {len(matches)}/{len(header_matches)} email(s) confirmé(s).")
                return matches
            try:
                status, msg_data = imap.fetch(b",".join(batch), "(BODY.PEEK[])")
            except OSError as exc:
                log(f"{len(batch)} e-mail(s) passé(s) : erreur en les lisant ({said(exc)}).")
                unread += len(batch)
                continue
            if status != "OK":
                log(f"{len(batch)} e-mail(s) passé(s) : le serveur mail a répondu {status} à leur lecture.")
                unread += len(batch)
                continue
            bodies_by_id = _parse_batched_fetch(msg_data)
            for mail_id in batch:
                full_bytes = bodies_by_id.get(mail_id)
                if full_bytes is None:
                    continue
                msg = email.message_from_bytes(full_bytes)

                timed_out_before = timeouts()
                if body_regex and not body_regex.search(_extract_text_body(msg)):
                    if timeouts() > timed_out_before:
                        slow.add(mail_id)
                    continue

                sender = _decode_header_value(msg.get("From"))
                subject = _decode_header_value(msg.get("Subject"))
                email_date = _parse_email_date(msg.get("Date"))
                attachments = _extract_attachments(msg, attachment_regex)
                if timeouts() > timed_out_before:
                    slow.add(mail_id)
                matches.append(
                    EmailMatch(
                        message_id=mail_id,
                        sender=sender,
                        subject=subject,
                        email_date=email_date,
                        attachments=attachments,
                    )
                )
                log(f"Retenu : « {subject} » de {sender} ({len(attachments)} pièce(s) jointe(s)).")
                if on_progress:
                    on_progress(len(matches), total)
        if unread or slow:
            raise IncompleteSearch(unread, matches, slow=len(slow))
    finally:
        imap.logout()

    return matches


UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows' device names: « NUL.pdf » or « COM1.pdf » is no file there.
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{kind}{n}" for kind in ("com", "lpt") for n in range(1, 10)}
)
#: A name's length on disk, its extension included: a sender's 300-character
#: name passed Windows' limit and failed the whole source (audit 04/10/2026).
MAX_NAME_LENGTH = 120


def attachment_file_name(name: str, taken: set[str]) -> str:
    """The name an attachment is written under: what the sender called it,
    with what a disk refuses replaced ("Facture 01/2026.pdf" raised, and the
    source stopped), made unique within the run - a second "facture.pdf"
    wrote over the first, and one invoice was lost without a word. Two runs
    fetching the same file are told apart at the import, by its digest."""
    cleaned = UNSAFE_NAME_RE.sub("_", name).strip().rstrip(". ") or "piece-jointe.pdf"
    stem, dot, extension = cleaned.rpartition(".")
    if not dot:
        stem, extension = cleaned, ""
    extension = extension[:10]
    stem = stem[: MAX_NAME_LENGTH - len(extension) - 1].rstrip(". ") or "piece-jointe"
    if stem.split(".")[0].strip().lower() in RESERVED_NAMES:
        stem = f"_{stem}"
    cleaned = f"{stem}.{extension}" if extension else stem
    candidate, number = cleaned, 1
    while candidate.lower() in taken:
        number += 1
        candidate = f"{stem} ({number}).{extension}" if extension else f"{stem} ({number})"
    taken.add(candidate.lower())
    return candidate


def scrape_email_invoices(
    download_dir: str,
    start_date: date,
    end_date: date,
    sender_pattern: str,
    subject_pattern: str = "",
    body_pattern: str = "",
    attachment_pattern: str = INVOICE_ATTACHMENT_PATTERN,
    log=print,
    on_progress=None,
    should_cancel=None,
) -> list[tuple[str, date | None]]:
    """Same matching as find_matching_emails, but writes every matched
    attachment to `download_dir` and returns (filepath, email_date) pairs -
    the same shape the old per-supplier scrapers returned, so this drops
    straight into the existing gather-and-import loop in tasks.py.

    An IncompleteSearch comes through with what WAS read written all the
    same, on its `downloaded`: the gather imports it and records nothing.
    An attachment the disk refuses (full, a file locked, a path too long)
    makes the search an incomplete one too (`unwritten`): the others are
    written and imported, and its mail is fetched again next time."""
    os.makedirs(download_dir, exist_ok=True)
    try:
        matches = find_matching_emails(
            start_date,
            end_date,
            sender_pattern,
            subject_pattern,
            body_pattern,
            attachment_pattern,
            log,
            on_progress,
            should_cancel,
        )
    except IncompleteSearch as incomplete:
        incomplete.downloaded, incomplete.unwritten = _write_attachments(download_dir, incomplete.matches, log)
        raise
    downloaded, unwritten = _write_attachments(download_dir, matches, log)
    if unwritten:
        incomplete = IncompleteSearch(0, matches, unwritten=unwritten)
        incomplete.downloaded = downloaded
        raise incomplete
    return downloaded


def _write_attachments(download_dir: str, matches, log) -> tuple[list[tuple[str, date | None]], int]:
    """(the files written, with their mail's date; how many attachments the
    disk refused)."""
    downloaded: list[tuple[str, date | None]] = []
    unwritten = 0
    taken: set[str] = set()
    for match in matches:
        for attachment in match.attachments:
            filepath = os.path.join(download_dir, attachment_file_name(attachment.filename, taken))
            try:
                with open(filepath, "wb") as f:
                    f.write(attachment.content)
            except OSError as exc:
                # One attachment the disk refuses is skipped, said, and the
                # source's other invoices still come in - counted: its mail
                # is not taken for nothing.
                log(f"Pièce jointe non enregistrée : {attachment.filename!r} ({exc.strerror or exc})")
                unwritten += 1
                continue
            log(f"Téléchargé : {attachment.filename}")
            downloaded.append((filepath, match.email_date))
    return downloaded, unwritten
