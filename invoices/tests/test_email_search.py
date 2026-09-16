"""The mailbox search's date range.

IMAP's BEFORE is exclusive ("internal date earlier than", RFC 3501 6.4.4),
so searching BEFORE the end date left out every email of the end date - and
the gather form's end date is today: an invoice emailed this morning was not
fetched until a later run.

No network: the IMAP client is replaced.
"""

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

    def test_a_range_of_one_day_finds_that_day(self):
        self.assertEqual(
            self.search_criteria(date(2026, 12, 31), date(2026, 12, 31)),
            'SINCE "31-Dec-2026" BEFORE "01-Jan-2027"',
        )
