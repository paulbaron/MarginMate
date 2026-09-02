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
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings

FETCH_TIMEOUT_SECONDS = 45  # per IMAP operation - independent of how many emails there are in total
BATCH_SIZE = 150  # messages per FETCH round trip - comfortably under IMAP servers' command-length limits
LOG_EVERY = 500  # scanned messages between liveness log lines, so a big date range doesn't look frozen


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
    attachment_pattern: str = r"(?i)\.pdf$",
    log=print,
    on_progress=None,
    should_cancel=None,
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
    """
    address = getattr(settings, "INVOICE_EMAIL_ADDRESS", "")
    app_password = getattr(settings, "INVOICE_EMAIL_APP_PASSWORD", "")
    if not address or not app_password:
        raise RuntimeError("INVOICE_EMAIL_ADDRESS / INVOICE_EMAIL_APP_PASSWORD are not configured in .env")

    sender_regex = re.compile(sender_pattern)
    subject_regex = re.compile(subject_pattern) if subject_pattern else None
    body_regex = re.compile(body_pattern) if body_pattern else None
    attachment_regex = re.compile(attachment_pattern or r"(?i)\.pdf$")

    matches: list[EmailMatch] = []

    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=FETCH_TIMEOUT_SECONDS)
    imap.login(address, app_password)
    try:
        imap.select("inbox")
        search_criteria = f'SINCE "{_format_date_for_imap(start_date)}" BEFORE "{_format_date_for_imap(end_date)}"'
        status, messages = imap.search(None, search_criteria)
        if status != "OK" or not messages or not messages[0]:
            log("No emails found in that date range")
            return matches

        mail_ids = messages[0].split()
        total = len(mail_ids)
        log(f"Scanning {total} email(s) between {start_date} and {end_date}")
        if on_progress:
            on_progress(0, total)

        # Phase 1: batch-fetch headers only, test sender_pattern/subject_pattern.
        header_matches: list[bytes] = []
        scanned = 0
        for batch in _chunked(mail_ids, BATCH_SIZE):
            if should_cancel and should_cancel():
                log(f"Recherche annulée après {scanned}/{total} email(s) analysé(s).")
                return matches
            try:
                status, header_data = imap.fetch(b",".join(batch), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            except OSError as exc:
                log(f"Skipping a batch of {len(batch)} email(s) after a header fetch error: {exc}")
                scanned += len(batch)
                continue
            if status != "OK":
                scanned += len(batch)
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
                continue
            if status != "OK":
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
                        message_id=mail_id, sender=sender, subject=subject, email_date=email_date, attachments=attachments
                    )
                )
                log(f"Matched: {subject!r} from {sender!r} ({len(attachments)} attachment(s))")
                if on_progress:
                    on_progress(len(matches), total)
    finally:
        imap.logout()

    return matches


def scrape_email_invoices(
    download_dir: str,
    start_date: date,
    end_date: date,
    sender_pattern: str,
    subject_pattern: str = "",
    body_pattern: str = "",
    attachment_pattern: str = r"(?i)\.pdf$",
    log=print,
    on_progress=None,
    should_cancel=None,
) -> list[tuple[str, date | None]]:
    """Same matching as find_matching_emails, but writes every matched
    attachment to `download_dir` and returns (filepath, email_date) pairs -
    the same shape the old per-supplier scrapers returned, so this drops
    straight into the existing gather-and-import loop in tasks.py."""
    os.makedirs(download_dir, exist_ok=True)
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
    downloaded: list[tuple[str, date | None]] = []
    for match in matches:
        for attachment in match.attachments:
            filepath = os.path.join(download_dir, attachment.filename)
            with open(filepath, "wb") as f:
                f.write(attachment.content)
            log(f"Downloaded: {attachment.filename}")
            downloaded.append((filepath, match.email_date))
    return downloaded
