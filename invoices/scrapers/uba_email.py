"""IMAP scraper for UBA invoices sent by email, ported from ScrapBarInvoices."""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
from datetime import date

from django.conf import settings

FETCH_TIMEOUT_SECONDS = 45  # per email - independent of how many emails there are in total


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


def scrape_uba_invoices(
    download_dir: str, start_date: date, end_date: date, log=print, on_progress=None
) -> list[tuple[str, date | None]]:
    """Returns a list of (pdf_path, email_date) tuples. ``email_date`` is used
    as a fallback invoice date if the PDF text doesn't yield one.
    """
    if not settings.UBA_EMAIL_ADDRESS or not settings.UBA_EMAIL_APP_PASSWORD:
        raise RuntimeError("UBA_EMAIL_ADDRESS / UBA_EMAIL_APP_PASSWORD are not configured in .env")

    os.makedirs(download_dir, exist_ok=True)
    downloaded: list[tuple[str, date | None]] = []

    # timeout bounds each individual socket operation (one fetch, one login,
    # ...) rather than the connection's total lifetime, so a big date range
    # with hundreds of emails is free to take as long as it needs overall.
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=FETCH_TIMEOUT_SECONDS)
    imap.login(settings.UBA_EMAIL_ADDRESS, settings.UBA_EMAIL_APP_PASSWORD)
    try:
        imap.select("inbox")
        search_criteria = (
            'FROM "Notifications@uba.paris" SUBJECT "Facture" '
            f'SINCE "{_format_date_for_imap(start_date)}" BEFORE "{_format_date_for_imap(end_date)}"'
        )
        status, messages = imap.search(None, search_criteria)
        if status != "OK" or not messages or not messages[0]:
            log("No new UBA invoice emails found")
            return downloaded

        mail_ids = messages[0].split()
        total = len(mail_ids)
        log(f"Found {total} UBA invoice email(s) between {start_date} and {end_date}")
        if on_progress:
            on_progress(0, total)
        for position, mail_id in enumerate(mail_ids, start=1):
            try:
                status, msg_data = imap.fetch(mail_id, "(RFC822)")
            except OSError as exc:
                # A single stalled/slow fetch (timeout, dropped connection, ...)
                # shouldn't take the rest of the batch down with it.
                log(f"Skipping email {position}/{total} after a fetch error: {exc}")
                continue
            if status != "OK":
                continue
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                msg = email.message_from_bytes(response_part[1])
                email_date = _parse_email_date(msg.get("Date"))
                if not msg.is_multipart():
                    continue
                for part in msg.walk():
                    if part.get_content_disposition() != "attachment":
                        continue
                    filename = part.get_filename()
                    if not filename or not filename.lower().endswith(".pdf"):
                        continue
                    filepath = os.path.join(download_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(part.get_payload(decode=True))
                    log(f"Downloaded UBA invoice: {filename}")
                    downloaded.append((filepath, email_date))
            if on_progress:
                on_progress(len(downloaded), total)
    finally:
        imap.logout()

    return downloaded
