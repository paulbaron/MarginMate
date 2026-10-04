"""A source's coverage moves only past what its search READ and imported
(invoices/coverage.py): a search the mail server answered in part, a mail a
pattern was too slow on, an attachment the disk refused, or a document left
unimported for want of the OCR, PDFium or the database, leaves the source's
line in error and its coverage where it was (rereview Q0, Q1, merge review).

No gather ever runs for real: the IMAP client is a fake (FakeMailbox), the
imports are replaced where a test says so, the heartbeat too. Every name and
date is invented.
"""

import os
import shutil
import tempfile
import threading
from unittest import mock

from django.db import OperationalError
from django.test import override_settings

from invoices import ocr
from invoices.importing import DuplicateInvoiceError
from invoices.models import INVOICE_ATTACHMENT_PATTERN, EmailInvoiceSource, GatherCoverage, InvoiceType, ScrapeJob
from invoices.scrapers import generic_email
from invoices.tasks import OVERLAP_DAYS, gather_invoices_task
from invoices.tests.test_auto_gather_coverage import CoverageCase
from invoices.tests.test_email_search import INCOMPLETE, FakeMailbox, SlowOn, dated_mail
from returnables import patterns

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

    def test_a_pattern_too_slow_on_a_mail_imports_the_rest_and_records_nothing(self):
        """[merge review] A match timing out was « no match »: the mail was
        left out, the search counted as whole, and the coverage moved past
        it for good. The older mail's attachment name, then its subject,
        takes the pattern too long."""
        real = patterns.check_invoice_mail_pattern
        for field, slow in (("attachment_pattern", INVOICE_ATTACHMENT_PATTERN), ("subject_pattern", "Facture")):
            with self.subTest(field=field):
                EmailInvoiceSource.objects.filter(invoice_type=self.cave).update(**{field: slow})

                def check(text, slow=slow, **kwargs):
                    compiled = real(text, **kwargs)
                    return SlowOn(compiled, "ancienne") if text == slow else compiled

                with mock.patch("returnables.patterns.check_invoice_mail_pattern", check):
                    job, imported, emit = self.run_with("")
                self.assertEqual(imported, ["recente.pdf"])
                self.assertEqual(
                    job.progress[self.email_code]["error"],
                    "Boîte mail : Recherche incomplète : motif trop lent sur 1 e-mail(s).",
                )
                self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(21))
                self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))


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

    def test_a_pattern_too_slow_on_a_mail_stores_the_rest_and_records_nothing(self):
        """[merge review] The format's attachment pattern takes too long on
        the older slip's name: that mail is not taken for nothing."""
        real = patterns.compile_pattern

        def compile_pattern(text, **kwargs):
            return SlowOn(real(text, **kwargs), "T0001")

        with mock.patch("returnables.patterns.compile_pattern", compile_pattern):
            job, stored, emit = self.run_with("")
        self.assertEqual(stored, ["T0002.pdf"])
        self.assertEqual(
            job.progress[self.slips_code]["error"],
            "Boîte mail : Recherche incomplète : motif trop lent sur 1 e-mail(s).",
        )
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, self.days_ago(21))
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))


class UnwrittenAttachmentTests(CoverageCase):
    """[merge review] An attachment the disk refuses (full, a file locked, a
    path too long) was skipped, said in the log only, and the coverage moved
    past its mail. What was written is imported; the line is in error and
    the range searched again."""

    def setUp(self):
        super().setUp()
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(14))

    def test_the_rest_is_imported_and_nothing_recorded(self):
        matches = [
            generic_email.EmailMatch(
                str(number).encode(),
                "factures@cave.exemple",
                "Facture",
                self.days_ago(days),
                [generic_email.EmailAttachment(name, b"%PDF-1.4 exemple")],
            )
            for number, (name, days) in enumerate((("refusee.pdf", 10), ("facture.pdf", 2)), start=1)
        ]
        real_open = open

        def refusing_open(path, *args, **kwargs):
            if os.path.basename(str(path)) == "refusee.pdf":
                raise OSError(28, "No space left on device")
            return real_open(path, *args, **kwargs)

        imported = []
        with (
            mock.patch("invoices.scrapers.generic_email.find_matching_emails", return_value=matches),
            mock.patch("invoices.scrapers.generic_email.open", refusing_open, create=True),
            mock.patch(
                "invoices.tasks._import_document_file",
                side_effect=lambda job, supplier, path, **kwargs: imported.append(os.path.basename(path)) or True,
            ),
        ):
            job, _email, _slips, emit = self.gather(
                {self.email_code}, email={"side_effect": generic_email.scrape_email_invoices}
            )
        self.assertEqual(imported, ["facture.pdf"])
        self.assertEqual(
            job.progress[self.email_code]["error"],
            "Boîte mail : Recherche incomplète : 1 pièce(s) jointe(s) non enregistrée(s) sur le disque.",
        )
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(14))
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))
        self.assertLessEqual(self.email_start(), self.days_ago(10))


@override_settings(INVOICE_EMAIL_ADDRESS="factures@example.test", INVOICE_EMAIL_APP_PASSWORD="x")
class RefusedStoredPatternTests(CoverageCase):
    """[merge review] A source saved before the motif guard, with a pattern it
    now refuses, failed every run as « Boîte mail : Motif de mail : … »: the
    mailbox blamed, and not a word of which of its four patterns."""

    def test_the_line_names_the_source_s_pattern_to_correct(self):
        EmailInvoiceSource.objects.filter(invoice_type=self.cave).update(body_pattern="Votre {document")
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(5))
        job = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC, auto_gather_id=self.rule.pk)
        with (
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]),
            mock.patch("notifications.events.emit") as emit,
        ):
            gather_invoices_task(job.id, None, None, {self.email_code}, False, unattended=True)
        job.refresh_from_db()
        error = job.progress[self.email_code]["error"]
        self.assertTrue(error.startswith("Motif de la source à corriger : Motif de contenu : accolade"), error)
        client.assert_not_called()
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(5))
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))


class FailedImportTests(CoverageCase):
    """[Q1] A document fetched and not imported for want of the OCR or the
    database is fetched again: the line is in error and the coverage stays.
    One refused for what it is (a duplicate, a parser that cannot read it)
    lets the coverage move - retried for ever, it would hold the source back
    and send a failed alert every day."""

    def setUp(self):
        super().setUp()
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(14))
        # A file on disk: a reader's own documents are refused by their digest
        # first (tasks._import_downloaded_file), which reads the file.
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        fetched = os.path.join(folder, "facture-10.pdf")
        with open(fetched, "wb") as handle:
            handle.write(b"%PDF-1.4 facture exemple")
        self.fetched = {"return_value": [(fetched, self.days_ago(10))]}

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

    def pdfium_held(self):
        """PDFIUM_LOCK held by another thread (a folder import drawing a heavy
        document) past what a document waits for it."""
        held, release = threading.Event(), threading.Event()

        def hold():
            with ocr.PDFIUM_LOCK:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(10))
        return mock.patch.object(ocr, "PDFIUM_WAIT_SECONDS", 0.05)

    @staticmethod
    def drawn(path, *args, **kwargs):
        """A reader drawing the document's pages, as its OCR does."""
        return list(ocr._drawn_pages(path))

    def test_pdfium_busy_on_a_document_read_like_any(self):
        """[merge review] PDFium busy was taken for a refusal of the document
        (« Failed to import »): the coverage moved past it for good."""
        with self.pdfium_held(), mock.patch("invoices.receipts.import_document", side_effect=self.drawn):
            job, _email, _slips, emit = self.gather({self.email_code}, email=self.fetched)
        self.assert_fetched_again(job, emit)
        self.assertIn("Not imported now, fetched again next time", job.log)

    def test_pdfium_busy_on_a_document_of_a_reader_of_its_own(self):
        InvoiceType.objects.filter(pk=self.cave.pk).update(parser_key="cave_exemple")
        with self.pdfium_held(), mock.patch("invoices.tasks.parse_and_import", side_effect=self.drawn):
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
