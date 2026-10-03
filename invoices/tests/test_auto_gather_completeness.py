"""A source's coverage moves only past what its search READ and imported
(invoices/coverage.py): a search the mail server answered in part, or a
document left unimported for want of the OCR or the database, leaves the
source's line in error and its coverage where it was (rereview Q0, Q1).

No gather ever runs for real: the IMAP client is a fake (FakeMailbox), the
imports are replaced where a test says so, the heartbeat too. Every name and
date is invented.
"""

from unittest import mock

from django.db import OperationalError
from django.test import override_settings

from invoices.importing import DuplicateInvoiceError
from invoices.models import GatherCoverage, InvoiceType, ScrapeJob
from invoices.tasks import OVERLAP_DAYS, gather_invoices_task
from invoices.tests.test_auto_gather_coverage import CoverageCase
from invoices.tests.test_email_search import INCOMPLETE, FakeMailbox, dated_mail

#: What the seeded slip format's sender and subject patterns take.
SLIP_SUBJECT = "Livraison du 10/02/2026 Tour. : ZZZ Compte : 00000"
SLIP_SENDER = "mphone@uba.paris"


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class IncompleteMailboxSearchTests(CoverageCase):
    """[Q0] A mailbox type: what was read is imported, the rest is said."""

    def setUp(self):
        super().setUp()
        # Three weeks without a whole search (an outage).
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(21))

    def run_with(self, broken):
        box = FakeMailbox(
            {b"1": dated_mail(self.days_ago(15), "ancienne"), b"2": dated_mail(self.days_ago(1), "recente")}, broken
        )
        imported = []
        job = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC, auto_gather_id=self.rule.pk)
        with (
            mock.patch("invoices.scrapers.generic_email.BATCH_SIZE", 1),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch(
                "invoices.tasks._import_document_file",
                side_effect=lambda job, supplier, path, **kwargs: imported.append(path.replace("\\", "/")) or True,
            ),
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]),
            mock.patch("notifications.events.emit") as emit,
        ):
            box.install(client)
            gather_invoices_task(job.id, None, None, {self.email_code}, False, unattended=True)
        job.refresh_from_db()
        return job, [path.rsplit("/", 1)[-1] for path in imported], emit

    def test_a_whole_search_imports_both_and_moves_the_coverage(self):
        job, imported, _emit = self.run_with("")
        self.assertNotIn("error", job.progress[self.email_code])
        self.assertEqual(imported, ["ancienne.pdf", "recente.pdf"])
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def test_a_search_answered_in_part_imports_the_rest_and_records_nothing(self):
        for broken in INCOMPLETE:
            with self.subTest(broken=broken):
                job, imported, emit = self.run_with(broken)
                self.assertEqual(imported, ["recente.pdf"])
                self.assertEqual(
                    job.progress[self.email_code]["error"],
                    "Boîte mail : Recherche incomplète : 1 e-mail(s) non lu(s) par le serveur mail.",
                )
                self.assertEqual(job.progress[self.email_code]["imported"], 1)
                self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(21))
                self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))

    def test_a_search_answered_no_is_a_failed_source_not_an_empty_range(self):
        job, imported, emit = self.run_with("search-no")
        self.assertEqual(imported, [])
        self.assertEqual(
            job.progress[self.email_code]["error"], "Boîte mail : Recherche refusée par le serveur mail (NO)."
        )
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(21))
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))

    def test_the_next_run_searches_the_unread_mail_again(self):
        self.run_with("body-no")
        self.assertLessEqual(self.email_start(), self.days_ago(15))


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class IncompleteSlipsSearchTests(CoverageCase):
    """[Q0] A slip format the same way: what was read is stored."""

    def setUp(self):
        super().setUp()
        GatherCoverage.objects.create(code=self.slips_code, searched_until=self.days_ago(21))

    def run_with(self, broken):
        box = FakeMailbox(
            {
                b"1": dated_mail(self.days_ago(15), "T0001", sender=SLIP_SENDER, subject=SLIP_SUBJECT),
                b"2": dated_mail(self.days_ago(1), "T0002", sender=SLIP_SENDER, subject=SLIP_SUBJECT),
            },
            broken,
        )
        stored = []

        def store(fmt, matches, log, *, progress=None):
            stored.extend(attachment.filename for match in matches for attachment in match.attachments)
            return len(matches), len(matches), ""

        job = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC, auto_gather_id=self.rule.pk)
        with (
            mock.patch("invoices.scrapers.generic_email.BATCH_SIZE", 1),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch("returnables.mail.store_matches", side_effect=store),
            mock.patch("notifications.events.emit") as emit,
        ):
            box.install(client)
            gather_invoices_task(job.id, None, None, {self.slips_code}, False, unattended=True)
        job.refresh_from_db()
        return job, stored, emit

    def test_a_whole_search_stores_both_and_moves_the_coverage(self):
        job, stored, _emit = self.run_with("")
        self.assertNotIn("error", job.progress[self.slips_code])
        self.assertEqual(stored, ["T0001.pdf", "T0002.pdf"])
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, self.today)

    def test_a_search_answered_in_part_stores_the_rest_and_records_nothing(self):
        for broken in INCOMPLETE:
            with self.subTest(broken=broken):
                job, stored, emit = self.run_with(broken)
                self.assertEqual(stored, ["T0002.pdf"])
                self.assertEqual(
                    job.progress[self.slips_code]["error"],
                    "Boîte mail : Recherche incomplète : 1 e-mail(s) non lu(s) par le serveur mail.",
                )
                self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, self.days_ago(21))
                self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))

    def test_a_search_answered_no_records_nothing(self):
        job, stored, _emit = self.run_with("search-no")
        self.assertEqual(stored, [])
        self.assertIn("error", job.progress[self.slips_code])
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, self.days_ago(21))


class FailedImportTests(CoverageCase):
    """[Q1] A document fetched and not imported for want of the OCR or the
    database is fetched again: the line is in error and the coverage stays.
    One refused for what it is (a duplicate, a parser that cannot read it)
    lets the coverage move - retried for ever, it would hold the source back
    and send a failed alert every day."""

    def setUp(self):
        super().setUp()
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(14))
        self.fetched = {"return_value": [("/nowhere/facture-10.pdf", self.days_ago(10))]}

    def assert_fetched_again(self, job, emit):
        self.assertIn("error", job.progress[self.email_code])
        self.assertIn("repris à la prochaine récupération", job.progress[self.email_code]["error"])
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(14))
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))
        self.assertLessEqual(self.email_start(), self.days_ago(10))

    def assert_coverage_moved(self, job):
        self.assertNotIn("error", job.progress[self.email_code])
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def test_the_ocr_lock_never_free(self):
        with mock.patch("invoices.receipts.OCR_LOCK") as lock:
            lock.acquire.return_value = False
            job, _email, _slips, emit = self.gather({self.email_code}, email=self.fetched)
        self.assert_fetched_again(job, emit)

    def test_by_hand_too_the_source_is_in_error_and_its_period_offered_again(self):
        """[S4] A manual gather is no exception: the document was fetched and
        not imported, so the run's period is offered again - left clean, the
        next default start (the newest invoice brought in) skipped it."""
        from django.urls import reverse

        from invoices import workspace

        with mock.patch("invoices.receipts.OCR_LOCK") as lock:
            lock.acquire.return_value = False
            job, *_ = self.gather({self.email_code}, unattended=False, start=self.days_ago(12), email=self.fetched)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertIn("repris à la prochaine récupération", job.progress[self.email_code]["error"])
        self.assertIn(self.email_code, workspace._missed(job))
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(14))
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertEqual(page.context["default_start_date"], job.range_start)
        self.assertEqual(job.range_start, self.days_ago(12))

    def test_the_database_locked_on_a_document_read_like_any(self):
        with mock.patch("invoices.receipts.import_document", side_effect=OperationalError("database is locked")):
            job, _email, _slips, emit = self.gather({self.email_code}, email=self.fetched)
        self.assert_fetched_again(job, emit)

    def test_the_database_locked_on_a_document_of_a_reader_of_its_own(self):
        InvoiceType.objects.filter(pk=self.cave.pk).update(parser_key="cave_exemple")
        with mock.patch("invoices.tasks.parse_and_import", side_effect=OperationalError("database is locked")):
            job, _email, _slips, emit = self.gather({self.email_code}, email=self.fetched)
        self.assert_fetched_again(job, emit)

    def test_a_duplicate_lets_the_coverage_move(self):
        with mock.patch("invoices.receipts.import_document", side_effect=DuplicateInvoiceError("déjà importée")):
            job, *_ = self.gather({self.email_code}, email=self.fetched)
        self.assert_coverage_moved(job)

    def test_a_document_its_reader_cannot_read_lets_the_coverage_move(self):
        with mock.patch("invoices.receipts.import_document", side_effect=ValueError("illisible")):
            job, *_ = self.gather({self.email_code}, email=self.fetched)
        self.assert_coverage_moved(job)

    def test_a_later_run_importing_it_moves_the_coverage(self):
        with mock.patch("invoices.receipts.OCR_LOCK") as lock:
            lock.acquire.return_value = False
            self.gather({self.email_code}, email=self.fetched)
        with mock.patch("invoices.receipts.import_document"):
            job, *_ = self.gather({self.email_code}, email=self.fetched)
        self.assert_coverage_moved(job)
        self.assertEqual(self.email_start(), self.days_ago(OVERLAP_DAYS))
