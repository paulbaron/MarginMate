"""The mailbox search's date range.

IMAP's BEFORE is exclusive ("internal date earlier than", RFC 3501 6.4.4),
so searching BEFORE the end date left out every email of the end date - and
the gather form's end date is today: an invoice emailed this morning was not
fetched until a later run.

No network: the IMAP client is replaced.
"""

import os
import re
import tempfile
from datetime import date
from unittest import mock

from django.test import SimpleTestCase, override_settings

from invoices.scrapers.generic_email import find_matching_emails


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class DateRangeTests(SimpleTestCase):
    def search_criteria(self, start, end):
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            client.return_value.search.return_value = ("OK", [b""])
            find_matching_emails(start, end, sender_pattern=".", log=lambda message: None)
        return client.return_value.search.call_args.args[1]

    def test_the_end_date_is_included(self):
        self.assertEqual(
            self.search_criteria(date(2026, 9, 1), date(2026, 9, 16)),
            'SINCE "01-Sep-2026" BEFORE "17-Sep-2026"',
        )

    @override_settings(INVOICE_IMAP_HOST="imap.exemple.fr")
    def test_the_mailbox_is_wherever_the_bar_says(self):
        """Any provider, not Gmail only: the host was written into the
        fetcher, and a bar whose mail is elsewhere could not gather."""
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            client.return_value.search.return_value = ("OK", [b""])
            find_matching_emails(date(2026, 9, 1), date(2026, 9, 2), sender_pattern=".", log=lambda message: None)
        self.assertEqual(client.call_args.args[0], "imap.exemple.fr")

    def test_a_range_of_one_day_finds_that_day(self):
        self.assertEqual(
            self.search_criteria(date(2026, 12, 31), date(2026, 12, 31)),
            'SINCE "31-Dec-2026" BEFORE "01-Jan-2027"',
        )


def fake_mailbox(client, message_bytes: bytes) -> None:
    """The IMAP client's answers for ONE message in the range: its headers
    (phase 1), then the whole message (phase 2), each shaped as imaplib
    returns a FETCH - (info, content) tuples and a closing b")"."""
    headers = message_bytes.split(b"\n\n", 1)[0] + b"\n\n"
    client.return_value.search.return_value = ("OK", [b"1"])
    client.return_value.fetch.side_effect = [
        ("OK", [(b"1 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}" % len(headers), headers), b")"]),
        ("OK", [(b"1 (BODY[] {%d}" % len(message_bytes), message_bytes), b")"]),
    ]


def slip_mail(content: bytes, sender="mphone@uba.paris") -> bytes:
    """A driver's mail as UBA sends it: one PDF attached as
    application/octet-stream. Tour, account, day and ticket invented."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = "factures@example.test"
    message["Subject"] = "Livraison du 10/02/2026 Tour. : ZZZ Compte : 00000"
    message["Date"] = "Tue, 10 Feb 2026 08:15:02 +0100"
    message.set_content("Veuillez trouver ci-joint une copie de votre ticket pour la livraison du 10/02/2026")
    message.add_attachment(content, maintype="application", subtype="octet-stream", filename="T00000000000000001.pdf")
    return message.as_bytes()


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class CompiledPatternsTests(SimpleTestCase):
    """`compile=`: how the patterns become matchers. Left out,
    returnables.patterns.invoice_mail_matcher - `re`'s meaning, checked and
    timed - for every invoice source and « Tester »; the returnables gather
    passes returnables.patterns.mail_matcher (checked, case-insensitive, timed)."""

    def search(self, message_bytes, **kwargs):
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            fake_mailbox(client, message_bytes)
            return find_matching_emails(date(2026, 2, 1), date(2026, 2, 28), log=lambda message: None, **kwargs)

    def test_a_slip_octet_stream_attachment_comes_through(self):
        """UBA's driver attaches his ticket as application/octet-stream: the
        attachment is taken by its disposition and name, not its type."""
        from returnables import patterns
        from returnables.tests.support import UBA_PATTERNS

        matches = self.search(
            slip_mail(b"%PDF-1.4 bon exemple"),
            sender_pattern=UBA_PATTERNS["sender_pattern"],
            subject_pattern=UBA_PATTERNS["subject_pattern"],
            attachment_pattern=UBA_PATTERNS["attachment_pattern"],
            compile=patterns.mail_matcher,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].subject, "Livraison du 10/02/2026 Tour. : ZZZ Compte : 00000")
        self.assertEqual(matches[0].email_date, date(2026, 2, 10))
        self.assertEqual(
            [(attachment.filename, attachment.content) for attachment in matches[0].attachments],
            [("T00000000000000001.pdf", b"%PDF-1.4 bon exemple")],
        )

    def test_left_out_it_is_re_as_before(self):
        """Case-sensitive, as an invoice source's patterns always were."""
        message = slip_mail(b"%PDF-1.4 bon exemple", sender="MPHONE@UBA.PARIS")
        self.assertEqual(self.search(message, sender_pattern=r"mphone@uba\.paris"), [])
        from returnables import patterns

        self.assertEqual(
            len(self.search(message, sender_pattern=r"mphone@uba\.paris", compile=patterns.mail_matcher)), 1
        )

    def test_left_out_a_pattern_the_guard_refuses_stops_before_signing_in(self):
        """A bare re.compile took any pattern; a counted repetition that
        large is refused before anything signs in (audit 04/10/2026)."""
        from returnables.patterns import PatternError

        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            with self.assertRaises(PatternError):
                find_matching_emails(
                    date(2026, 2, 1), date(2026, 2, 28), sender_pattern="(?:x{500}){500}", log=lambda message: None
                )
        client.assert_not_called()

    def test_left_out_a_body_that_makes_the_pattern_backtrack_is_no_match(self):
        """A body anybody can write must not hang the gather: the match
        times out, the mail is left out, and the log says why."""
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = "factures@exemple.fr"
        message["Subject"] = "Facture"
        message["Date"] = "Tue, 10 Feb 2026 08:15:02 +0100"
        message.set_content("a" * 60 + "b")
        logged = []
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            fake_mailbox(client, message.as_bytes())
            matches = find_matching_emails(
                date(2026, 2, 1),
                date(2026, 2, 28),
                sender_pattern="exemple",
                body_pattern="(a|aa)+$",
                log=logged.append,
            )
        self.assertEqual(matches, [])
        self.assertTrue(any("trop lent" in line for line in logged))

    def test_every_pattern_goes_through_it(self):
        compiled = []

        def compile(pattern):
            compiled.append(pattern)
            return re.compile(pattern)

        self.search(
            slip_mail(b"%PDF-1.4"),
            sender_pattern="uba",
            subject_pattern="Livraison",
            body_pattern="ticket",
            attachment_pattern=r"\.pdf$",
            compile=compile,
        )
        self.assertEqual(compiled, ["uba", "Livraison", "ticket", r"\.pdf$"])

    def test_a_pattern_it_refuses_stops_the_search_before_signing_in(self):
        from returnables.patterns import PatternError

        def refuse(pattern):
            raise PatternError("Motif de mail : refusé.")

        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client:
            with self.assertRaises(PatternError):
                find_matching_emails(
                    date(2026, 2, 1), date(2026, 2, 28), sender_pattern="x", compile=refuse, log=lambda message: None
                )
        client.assert_not_called()


def dated_mail(day: date, name: str, sender="factures@cave.exemple", subject=None) -> bytes:
    """A mail of `day` from `sender` carrying `<name>.pdf`. Data invented."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = "factures@example.test"
    message["Subject"] = subject or f"Facture {name}"
    message["Date"] = day.strftime("%a, %d %b %Y 08:00:00 +0200")
    message.set_content("Votre document.")
    message.add_attachment(b"%PDF-1.4 exemple", maintype="application", subtype="pdf", filename=f"{name}.pdf")
    return message.as_bytes()


class FakeMailbox:
    """The IMAP client's answers for `mails` (id -> the message's bytes), read
    one message a batch (BATCH_SIZE patched to 1). `broken` is what goes wrong,
    on mail b"1" only: « search-no » (the SEARCH answered NO), « header-no » /
    « body-no » (that FETCH answered NO, as Gmail answers « Some messages could
    not be FETCHed »), « header-timeout » / « body-timeout » (the FETCH timed
    out - an OSError the connection recovers from)."""

    def __init__(self, mails: dict, broken: str = ""):
        self.mails = mails
        self.broken = broken

    def install(self, client) -> None:
        client.return_value.search.side_effect = self.search
        client.return_value.fetch.side_effect = self.fetch

    def search(self, charset, criteria):
        if self.broken == "search-no":
            return "NO", [b"[UNAVAILABLE] Temporary System Error"]
        return "OK", [b" ".join(self.mails)]

    def fetch(self, message_set, parts):
        phase = "header" if "HEADER" in parts else "body"
        if message_set == b"1" and self.broken == f"{phase}-no":
            return "NO", [b"Some messages could not be FETCHed (Failure)"]
        if message_set == b"1" and self.broken == f"{phase}-timeout":
            raise TimeoutError("The read operation timed out")
        data = []
        for mail_id in message_set.split(b","):
            raw = self.mails[mail_id]
            content = raw.split(b"\n\n", 1)[0] + b"\n\n" if phase == "header" else raw
            data += [(mail_id + b" (BODY[] {%d}" % len(content), content), b")"]
        return "OK", data


#: Every way a server answers part of a search and not the rest.
INCOMPLETE = ("header-no", "body-no", "header-timeout", "body-timeout")


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class IncompleteSearchTests(SimpleTestCase):
    """A server answering a FETCH « NO », or a FETCH timing out, left the
    batch unread and the search returned normally: the gather took it for a
    whole search and recorded the source covered past mails it never read
    (rereview Q0). The rest is still read; the search says it is
    incomplete. A SEARCH answered NO is no empty range."""

    def mails(self):
        return {b"1": dated_mail(date(2026, 3, 2), "ancienne"), b"2": dated_mail(date(2026, 3, 20), "recente")}

    def search(self, broken=""):
        with (
            mock.patch("invoices.scrapers.generic_email.BATCH_SIZE", 1),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
        ):
            FakeMailbox(self.mails(), broken).install(client)
            self.client_mock = client
            return find_matching_emails(
                date(2026, 3, 1), date(2026, 3, 31), sender_pattern=r"cave\.exemple", log=lambda message: None
            )

    def test_a_whole_search_reads_both(self):
        self.assertEqual([match.subject for match in self.search()], ["Facture ancienne", "Facture recente"])

    def test_a_search_answered_no_is_no_empty_range(self):
        from invoices.scrapers import generic_email

        with self.assertRaises(RuntimeError) as caught:
            self.search("search-no")
        self.assertNotIsInstance(caught.exception, generic_email.IncompleteSearch)
        self.assertEqual(str(caught.exception), "Recherche refusée par le serveur mail (NO).")
        self.client_mock.return_value.logout.assert_called_once()

    def test_a_batch_left_unread_makes_the_search_incomplete_and_the_rest_is_read(self):
        from invoices.scrapers import generic_email

        for broken in INCOMPLETE:
            with self.subTest(broken=broken):
                with self.assertRaises(generic_email.IncompleteSearch) as caught:
                    self.search(broken)
                self.assertEqual(caught.exception.unread, 1)
                self.assertEqual([match.subject for match in caught.exception.matches], ["Facture recente"])
                self.assertEqual(
                    str(caught.exception), "Recherche incomplète : 1 e-mail(s) non lu(s) par le serveur mail."
                )
                self.client_mock.return_value.logout.assert_called_once()

    def test_scraping_writes_what_was_read_and_says_it_is_incomplete(self):
        from invoices.scrapers import generic_email

        folder = self.enterContext(tempfile.TemporaryDirectory())
        with (
            mock.patch("invoices.scrapers.generic_email.BATCH_SIZE", 1),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
        ):
            FakeMailbox(self.mails(), "body-no").install(client)
            with self.assertRaises(generic_email.IncompleteSearch) as caught:
                generic_email.scrape_email_invoices(
                    folder, date(2026, 3, 1), date(2026, 3, 31), sender_pattern=r"cave\.exemple", log=lambda m: None
                )
        self.assertEqual(
            [(os.path.basename(path), day) for path, day in caught.exception.downloaded],
            [("recente.pdf", date(2026, 3, 20))],
        )
        self.assertEqual(os.listdir(folder), ["recente.pdf"])


class AttachmentFileTests(SimpleTestCase):
    """What lands on disk before the import: one file per attachment, even
    when two share a name, and a name the disk refuses does not stop the
    source. Data invented."""

    def download(self, *attachments):
        from invoices.scrapers.generic_email import EmailAttachment, EmailMatch, scrape_email_invoices

        matches = [
            EmailMatch(
                message_id=str(number).encode(),
                sender="factures@exemple.fr",
                subject="Votre facture",
                email_date=date(2026, 5, number),
                attachments=[EmailAttachment(filename=name, content=content)],
            )
            for number, (name, content) in enumerate(attachments, start=1)
        ]
        folder = self.enterContext(tempfile.TemporaryDirectory())
        with mock.patch("invoices.scrapers.generic_email.find_matching_emails", return_value=matches):
            return folder, scrape_email_invoices(
                folder, date(2026, 5, 1), date(2026, 5, 31), sender_pattern=".", log=lambda m: None
            )

    def test_two_attachments_of_the_same_name_are_two_files(self):
        """The second "facture.pdf" wrote over the first: one invoice lost, silently."""
        _folder, files = self.download(("facture.pdf", b"%PDF-A"), ("facture.pdf", b"%PDF-B"))
        contents = [open(path, "rb").read() for path, _day in files]  # noqa: SIM115 - read at once, closed as it is dropped
        self.assertEqual(contents, [b"%PDF-A", b"%PDF-B"])

    def test_a_name_the_disk_refuses_is_written_under_a_safe_one(self):
        folder, files = self.download(("Facture 01/2026.pdf", b"%PDF-A"), ("fac:ture?*.pdf", b"%PDF-B"))
        self.assertEqual(len(files), 2)
        for path, _day in files:
            self.assertEqual(os.path.dirname(path), folder)
            self.assertTrue(os.path.basename(path).endswith(".pdf"))

    def test_a_name_too_long_for_the_disk_is_cut_and_keeps_its_extension(self):
        _folder, files = self.download(("F" * 300 + ".pdf", b"%PDF-A"))
        name = os.path.basename(files[0][0])
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".pdf"))

    def test_a_windows_device_name_is_written_under_another(self):
        _folder, files = self.download(("NUL.pdf", b"%PDF-A"), ("com1.pdf", b"%PDF-B"))
        self.assertEqual([os.path.basename(path) for path, _day in files], ["_NUL.pdf", "_com1.pdf"])

    def test_an_attachment_the_disk_refuses_does_not_stop_the_others(self):
        real_open = open

        def refusing_open(path, *args, **kwargs):
            if os.path.basename(str(path)) == "refuse.pdf":
                raise OSError(22, "Invalid argument")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", refusing_open):
            _folder, files = self.download(("refuse.pdf", b"%PDF-A"), ("facture.pdf", b"%PDF-B"))
        self.assertEqual([os.path.basename(path) for path, _day in files], ["facture.pdf"])
