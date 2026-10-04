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
    send the .env's password elsewhere."""
    from django.conf import settings

    from accounts import vault

    state = vault.load()
    stored = state.values
    address = stored.get("INVOICE_EMAIL_ADDRESS") or getattr(settings, "INVOICE_EMAIL_ADDRESS", "")
    if stored.get("INVOICE_EMAIL_APP_PASSWORD"):
        app_password = stored["INVOICE_EMAIL_APP_PASSWORD"]
        host = state.bindings.get("INVOICE_EMAIL_APP_PASSWORD", "")
        if not host:
            raise RuntimeError(MAILBOX_UNBOUND)
    else:
        app_password = getattr(settings, "INVOICE_EMAIL_APP_PASSWORD", "")
        host = getattr(settings, "INVOICE_IMAP_HOST", "") or DEFAULT_IMAP_HOST
    if not address or not app_password:
        raise RuntimeError(MAILBOX_MISSING)
    return address, app_password, host


FETCH_TIMEOUT_SECONDS = 45  # per IMAP operation - independent of how many emails there are in total
BATCH_SIZE = 150  # messages per FETCH round trip - comfortably under IMAP servers' command-length limits
LOG_EVERY = 500  # scanned messages between liveness log lines, so a big date range doesn't look frozen


#: Said when the server answers the date range's SEARCH with anything but OK:
#: a refusal is no empty range.
SEARCH_REFUSED = "Recherche refusée par le serveur mail ({status})."


class IncompleteSearch(RuntimeError):
    """The server left `unread` mails of the range unread (a FETCH answered
    NO, or timed out): the search read the rest - `matches` - and is not a
    whole one. The gather imports what it read and records no coverage
    (invoices/coverage.py): the range is searched again next time.
    `downloaded`: scrape_email_invoices' files for `matches`."""

    def __init__(self, unread: int, matches: list):
        super().__init__(f"Recherche incomplète : {unread} e-mail(s) non lu(s) par le serveur mail.")
        self.unread = unread
        self.matches = matches
        self.downloaded: list[tuple[str, date | None]] = []


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
    """Searches the shared invoice mailbox for emails matching every given
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

    The mailbox is the owner's: from a tenant that may not use the
    server's accounts, refused before the settings are read or anything
    signs in (invoices/integrations.py) - scrape_email_invoices goes through
    here too.

    `compile` turns each pattern (sender, subject, body, attachment) into
    something with `.search(text)`. By default (an invoice source's patterns,
    and « Tester ») returnables.patterns.invoice_mail_matcher: `re`'s meaning
    as before, but checked by the motif guard and matched with a timeout - a
    bare `re.compile` let one email's body hang the whole server on a
    backtracking pattern (security audit 04/10/2026). The returnables gather passes
    returnables.patterns.mail_matcher: a format's patterns are checked before
    anything compiles them, matched case-insensitively and with a timeout -
    a header anybody on the internet can write must not hang the gather. A
    pattern it refuses raises here, before anything signs in.
    """
    from accounts.tenancy import integrations_allowed
    from invoices import integrations

    if not integrations_allowed():
        raise RuntimeError(integrations.MAILBOX)
    address, app_password, host = mailbox_credentials()

    if compile is None:
        from functools import partial

        from returnables.patterns import invoice_mail_matcher

        compile = partial(invoice_mail_matcher, log=log)

    sender_regex = compile(sender_pattern)
    subject_regex = compile(subject_pattern) if subject_pattern else None
    body_regex = compile(body_pattern) if body_pattern else None
    attachment_regex = compile(attachment_pattern or INVOICE_ATTACHMENT_PATTERN)

    matches: list[EmailMatch] = []

    # A verifying context: imaplib's default (ssl._create_stdlib_context)
    # checks neither the certificate nor the host name, and the app password
    # went to whoever answered the TLS handshake (security review 01/10/2026).
    imap = imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(), timeout=FETCH_TIMEOUT_SECONDS)
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
            raise RuntimeError(SEARCH_REFUSED.format(status=status))
        if not messages or not messages[0]:
            log("No emails found in that date range")
            return matches

        mail_ids = messages[0].split()
        total = len(mail_ids)
        log(f"Scanning {total} email(s) between {start_date} and {end_date}")
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
                status, header_data = imap.fetch(b",".join(batch), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            except OSError as exc:
                log(f"Skipping a batch of {len(batch)} email(s) after a header fetch error: {exc}")
                scanned += len(batch)
                unread += len(batch)
                continue
            if status != "OK":
                log(f"Skipping a batch of {len(batch)} email(s): the server answered the header fetch {status}")
                scanned += len(batch)
                unread += len(batch)
                continue
            headers_by_id = _parse_batched_fetch(header_data)
            for mail_id in batch:
                scanned += 1
                header_bytes = headers_by_id.get(mail_id)
                if header_bytes is None:
                    continue
                header_msg = email.message_from_bytes(header_bytes)
                sender = _decode_header_value(header_msg.get("From"))
                subject = _decode_header_value(header_msg.get("Subject"))
                if not sender_regex.search(sender):
                    continue
                if subject_regex and not subject_regex.search(subject):
                    continue
                header_matches.append(mail_id)
            if on_progress:
                on_progress(len(header_matches), total)
            if scanned % LOG_EVERY < BATCH_SIZE:
                log(f"Scanned {scanned}/{total} email(s), {len(header_matches)} matched sender/subject so far")

        log(f"{len(header_matches)} email(s) matched sender/subject - fetching full content")

        # Phase 2: batch-fetch the full message only for header matches, test
        # body_pattern and pull attachments.
        for batch in _chunked(header_matches, BATCH_SIZE):
            if should_cancel and should_cancel():
                log(f"Recherche annulée après {len(matches)}/{len(header_matches)} email(s) confirmé(s).")
                return matches
            try:
                status, msg_data = imap.fetch(b",".join(batch), "(BODY.PEEK[])")
            except OSError as exc:
                log(f"Skipping a batch of {len(batch)} email(s) after a fetch error: {exc}")
                unread += len(batch)
                continue
            if status != "OK":
                log(f"Skipping a batch of {len(batch)} email(s): the server answered the fetch {status}")
                unread += len(batch)
                continue
            bodies_by_id = _parse_batched_fetch(msg_data)
            for mail_id in batch:
                full_bytes = bodies_by_id.get(mail_id)
                if full_bytes is None:
                    continue
                msg = email.message_from_bytes(full_bytes)

                if body_regex and not body_regex.search(_extract_text_body(msg)):
                    continue

                sender = _decode_header_value(msg.get("From"))
                subject = _decode_header_value(msg.get("Subject"))
                email_date = _parse_email_date(msg.get("Date"))
                attachments = _extract_attachments(msg, attachment_regex)
                matches.append(
                    EmailMatch(
                        message_id=mail_id,
                        sender=sender,
                        subject=subject,
                        email_date=email_date,
                        attachments=attachments,
                    )
                )
                log(f"Matched: {subject!r} from {sender!r} ({len(attachments)} attachment(s))")
                if on_progress:
                    on_progress(len(matches), total)
        if unread:
            raise IncompleteSearch(unread, matches)
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
    same, on its `downloaded`: the gather imports it and records nothing."""
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
        incomplete.downloaded = _write_attachments(download_dir, incomplete.matches, log)
        raise
    return _write_attachments(download_dir, matches, log)


def _write_attachments(download_dir: str, matches, log) -> list[tuple[str, date | None]]:
    downloaded: list[tuple[str, date | None]] = []
    taken: set[str] = set()
    for match in matches:
        for attachment in match.attachments:
            filepath = os.path.join(download_dir, attachment_file_name(attachment.filename, taken))
            try:
                with open(filepath, "wb") as f:
                    f.write(attachment.content)
            except OSError as exc:
                # One attachment the disk refuses is skipped, said, and the
                # source's other invoices still come in.
                log(f"Pièce jointe ignorée : {attachment.filename!r} ({exc.strerror or exc})")
                continue
            log(f"Downloaded: {attachment.filename}")
            downloaded.append((filepath, match.email_date))
    return downloaded
