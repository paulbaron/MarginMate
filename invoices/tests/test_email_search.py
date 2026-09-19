"""The mailbox search's date range.

IMAP's BEFORE is exclusive ("internal date earlier than", RFC 3501 6.4.4),
so searching BEFORE the end date left out every email of the end date - and
the gather form's end date is today: an invoice emailed this morning was not
fetched until a later run.

No network: the IMAP client is replaced.
"""

import os
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


class AttachmentFileTests(SimpleTestCase):
    """What lands on disk before the import: one file per attachment, even
    when two share a name, and a name the disk refuses does not stop the
    source. Data invented."""

    def download(self, *attachments):
        from invoices.scrapers.generic_email import EmailAttachment, EmailMatch, scrape_email_invoices

        matches = [
            EmailMatch(message_id=str(number).encode(), sender="factures@exemple.fr", subject="Votre facture",
                       email_date=date(2026, 5, number), attachments=[EmailAttachment(filename=name, content=content)])
            for number, (name, content) in enumerate(attachments, start=1)
        ]
        folder = self.enterContext(tempfile.TemporaryDirectory())
        with mock.patch("invoices.scrapers.generic_email.find_matching_emails", return_value=matches):
            return folder, scrape_email_invoices(folder, date(2026, 5, 1), date(2026, 5, 31), sender_pattern=".", log=lambda m: None)

    def test_two_attachments_of_the_same_name_are_two_files(self):
        """The second "facture.pdf" wrote over the first: one invoice lost, silently."""
        _folder, files = self.download(("facture.pdf", b"%PDF-A"), ("facture.pdf", b"%PDF-B"))
        contents = [open(path, "rb").read() for path, _day in files]
        self.assertEqual(contents, [b"%PDF-A", b"%PDF-B"])

    def test_a_name_the_disk_refuses_is_written_under_a_safe_one(self):
        folder, files = self.download(("Facture 01/2026.pdf", b"%PDF-A"), ('fac:ture?*.pdf', b"%PDF-B"))
        self.assertEqual(len(files), 2)
        for path, _day in files:
            self.assertEqual(os.path.dirname(path), folder)
            self.assertTrue(os.path.basename(path).endswith(".pdf"))
