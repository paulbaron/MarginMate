"""« Récupérer » for the slips: what the gather asks of the returnables app.

The mailbox is searched by the gather itself (invoices/tasks._gather_slips,
beside the invoice sources, contained like them): the IMAP connector, the
cancel and the containment live there. This module only says WHERE a
format's search starts (`fetch_start`) and what becomes of what it found
(`store_matches`), and never imports invoices.tasks - that module imports
this one, lazily, inside the task.

Everything found goes through the one writer, returnables.slips.store_slip
(« Reçu par mail »): never an invoice import - a slip read as a purchase files
its empties as positive purchase lines.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from returnables import comparison, notify, reading, slips
from returnables.models import Slip
from returnables.reading import clean_text

logger = logging.getLogger(__name__)

#: The days before the newest slip already brought in that are searched
#: again: a slip mailed just before it may not have been in the mailbox yet.
OVERLAP_DAYS = 3
#: How far back the first search goes, when no slip came by mail yet.
DEFAULT_LOOKBACK_DAYS = 90
#: Never further back than this, whatever the posted start: a period asked
#: on Achats years back would scan every mail since for its slips.
MAX_LOOKBACK_DAYS = 400


def lookback_floor(today: date) -> date:
    """The earliest day a format's mails are ever searched from
    (MAX_LOOKBACK_DAYS back): a stretch left to catch up before it starts
    there for the gather's coverage (invoices/coverage.searched)."""
    return today - timedelta(days=MAX_LOOKBACK_DAYS)


def fetch_start(fmt, posted_start: date | None, today: date) -> date:
    """Where the search for `fmt`'s slips starts: the posted start, or a few
    days before the newest slip this format brought in BY MAIL (its mail's
    date, today at the latest) when that is earlier - else, with none,
    DEFAULT_LOOKBACK_DAYS back. Never earlier than MAX_LOOKBACK_DAYS back.

    Only mailed slips move it: a slip dropped on the page by hand - an old one
    as well as today's - says nothing about which mails were read, and one
    recent upload would have moved the start past mails never fetched. A
    mail date in the future (a sender's clock) is left out for the same
    reason."""
    newest = (
        Slip.objects.filter(format=fmt, origin=Slip.Origin.MAIL, mail_date__lte=today)
        .order_by("-mail_date")
        .values_list("mail_date", flat=True)
        .first()
    )
    # comparison.shifted: a mail date a damaged archive brought in
    # (0001-01-01) is cut at the calendar's start, never an OverflowError -
    # the home page asks for this start at every drawing.
    own = comparison.shifted(newest, -OVERLAP_DAYS) if newest else today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    start = min(posted_start, own) if posted_start else own
    return max(start, lookback_floor(today))


def _in_mail_order(matches) -> list:
    """The mails oldest first (undated ones last, the search's order kept
    otherwise): a run stopped by an error has then stored the earliest,
    and the next run's start - the newest stored - does not skip the rest."""
    return sorted(matches, key=lambda match: (match.email_date is None, match.email_date or date.min))


def _note(log) -> str:
    """The latest pickup's comparison, in words - "" when there is none,
    or when it cannot be worked out (said in the log: a note is never an
    error, and never stops the slips just stored)."""
    try:
        return comparison.latest_note()
    except Exception as exc:
        logger.exception("Consignes : comparaison de la dernière reprise impossible")
        log(f"Bons de consignes : la dernière reprise n'a pas pu être comparée ({exc.__class__.__name__}).")
        return ""


def store_matches(fmt, matches, log, *, progress=None) -> tuple[int, int, str]:
    """Store every attachment of `matches` (the search's EmailMatch list) as
    a slip of `fmt`, « Reçu par mail », with its mail's sender, subject and
    date. Returns (found, imported, note): the slips found (a document that
    is no slip of this format, or too heavy, is not one), the new ones, and
    the latest pickup's comparison in words.

    Each outcome is a log line « <nom> : <message> »: a slip already there
    (the same bytes, or a re-send of the same slip) is no failure, and
    neither is a mail's other attachment - read with this format, no
    returnables part and no line (« pas un bon … — ignoré »). An attachment
    over 5 MB is not read. `progress(found, imported)` after each new slip.
    Anything unexpected raises (the gather says it on the format's line):
    the mails are taken oldest first, so what was stored before it is what
    the next run starts after.

    The slips created are handed to `notify.notify_slips` once, after the
    loop and before the note - also when the loop stops on an error: a slip
    stored is never fetched again, and its alert would be lost."""
    found = imported = 0
    created = []
    try:
        for match in _in_mail_order(matches):
            for attachment in match.attachments:
                name = clean_text(attachment.filename or "", slips.MAX_NAME_CHARS) or "piece-jointe.pdf"
                content = attachment.content or b""
                if len(content) > reading.MAX_PDF_BYTES:
                    log(f"{name} : pièce jointe ignorée — {reading.TOO_HEAVY}")
                    continue
                result = slips.store_slip(
                    content,
                    filename=name,
                    fmt=fmt,
                    origin=Slip.Origin.MAIL,
                    mail_sender=match.sender or "",
                    mail_subject=match.subject or "",
                    mail_date=match.email_date,
                    skip_non_slips=True,
                )
                log(f"{name} : {result.message}")
                if result.kind == slips.IGNORED:
                    continue
                found += 1
                if result.created:
                    imported += 1
                    created.append(result.slip)
                    if progress is not None:
                        progress(found, imported)
    finally:
        notify.notify_slips(created)  # never raises
    return found, imported, _note(log)
