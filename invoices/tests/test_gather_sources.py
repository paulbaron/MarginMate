"""The gather, one source at a time: what each kind of source does with what
it fetched, and what one source failing does to the others.

The mailbox, Metro and the portals are replaced (never a real one): what is
tested is the task around them. Data invented.
"""

import os
from datetime import date
from unittest import mock

from django.test import TestCase

from invoices.models import EmailInvoiceSource, InvoiceType, ScrapeJob
from invoices.tasks import gather_invoices_task
from tests.factories import make_invoice, make_supplier


def email_type(supplier, name, parser_key=""):
    invoice_type = InvoiceType.objects.create(
        supplier=supplier, name=name, parser_key=parser_key, source_kind=InvoiceType.SourceKind.EMAIL
    )
    EmailInvoiceSource.objects.create(invoice_type=invoice_type, sender_pattern="factures@")
    return invoice_type


class MetroInTheGatherTests(TestCase):
    """Metro's failure used to fail the whole gather: on 18/09 its firewall
    refused the sign-in and the mailbox and the five portals were never
    searched."""

    def setUp(self):
        self.venue = make_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        self.mail_type = email_type(self.venue, "Salle Exemple - Factures")
        self.codes = {"METRO", f"type-{self.mail_type.id}"}

    def gather(self, metro_outcome, codes=None, start=date(2026, 1, 1), **kwargs):
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_metro_invoices", side_effect=metro_outcome) as scrape_metro, mock.patch(
            "invoices.tasks.scrape_email_invoices", return_value=[]
        ) as scrape_email, mock.patch("invoices.tasks.parse_and_import") as parse_and_import:
            gather_invoices_task(job.id, start, date(2026, 9, 18), self.codes if codes is None else codes, **kwargs)
        job.refresh_from_db()
        return job, scrape_metro, scrape_email, parse_and_import

    def test_a_refusal_stays_on_metros_line_and_the_others_go_on(self):
        from invoices.scrapers.metro import MetroBlocked, blocked_message

        def refused(*args, **kwargs):
            raise MetroBlocked(blocked_message("#18.0000000.1700000000.00000abc"), reference="#18.0000000.1700000000.00000abc")

        job, _scrape_metro, scrape_email, _import = self.gather(refused)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        scrape_email.assert_called_once()
        self.assertIn("#18.0000000.1700000000.00000abc", job.progress["METRO"]["error"])
        self.assertIn(f"type-{self.mail_type.id}", job.progress)

    def test_what_landed_before_a_stop_is_imported(self):
        from invoices.scrapers.metro import MetroError

        def stopped(*args, **kwargs):
            raise MetroError("Metro : la page n'a pas répondu comme prévu.", files=["/tmp/134_52_1_x.pdf"])

        job, _scrape_metro, _scrape_email, parse_and_import = self.gather(stopped)
        self.assertEqual(parse_and_import.call_args.args[0], "/tmp/134_52_1_x.pdf")
        self.assertEqual(job.progress["METRO"]["imported"], 1)
        self.assertIn("pas répondu", job.progress["METRO"]["error"])

    def test_an_unexpected_failure_is_contained_too(self):
        def broken(*args, **kwargs):
            raise ValueError("inattendu")

        job, _scrape_metro, scrape_email, _import = self.gather(broken)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        scrape_email.assert_called_once()
        self.assertIn("error", job.progress["METRO"])

    def test_metro_left_alone_after_a_refusal_is_said_as_a_failure(self):
        from invoices.scrapers.metro import MetroPaused

        def paused(*args, **kwargs):
            raise MetroPaused("Metro a bloqué la connexion le 18/09 à 16:19.", until=None, after_block=True)

        job, *_ = self.gather(paused)
        self.assertIn("bloqué", job.progress["METRO"]["error"])

    def test_metro_signed_in_to_lately_is_only_a_note(self):
        """Not a failure: the gather of an hour ago brought Metro's in."""
        from invoices.scrapers.metro import MetroPaused

        def recent(*args, **kwargs):
            raise MetroPaused("Metro a déjà été consulté le 18/09 à 10:00.", until=None, after_block=False)

        job, *_ = self.gather(recent)
        self.assertNotIn("error", job.progress["METRO"])
        self.assertIn("déjà été consulté", job.progress["METRO"]["note"])
        self.assertEqual(job.failed_sources, [])

    def test_metro_is_contacted_only_when_named(self):
        """A gather started from a shell or a script (source_codes=None) used
        to sign in to Metro too - most of 31/08's sign-ins were such."""
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_metro_invoices") as scrape_metro, mock.patch(
            "invoices.tasks.scrape_email_invoices", return_value=[]
        ):
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), None)
        scrape_metro.assert_not_called()

    def test_one_sign_in_asked_for_passes_the_pause(self):
        _job, scrape_metro, *_ = self.gather(lambda *a, **k: [], metro_now=True)
        self.assertTrue(scrape_metro.call_args.kwargs["ignore_pause"])

    def test_metro_is_searched_from_its_newest_invoice_at_the_latest(self):
        """While Metro was paused, gathers of the other sources moved the
        offered start past its last invoice: the days between were never
        searched on Metro. Its rows already imported are not downloaded."""
        from invoices.models import Supplier

        make_invoice(supplier=Supplier.objects.get(code="METRO"), invoice_date=date(2026, 8, 28), invoice_number="134-052-1")
        _job, scrape_metro, *_ = self.gather(lambda *a, **k: [], start=date(2026, 9, 15))
        self.assertLessEqual(scrape_metro.call_args.args[1], date(2026, 8, 28))
        _job, scrape_metro, *_ = self.gather(lambda *a, **k: [], start=date(2026, 3, 1))
        self.assertEqual(scrape_metro.call_args.args[1], date(2026, 3, 1))

    def test_metros_own_start_counts_only_its_gathered_invoices(self):
        """A photographed Metro ticket, or a misread future date, dated after
        its last PDF moved Metro's start past the gap."""
        from invoices.models import Supplier

        metro = Supplier.objects.get(code="METRO")
        make_invoice(supplier=metro, invoice_date=date(2026, 6, 1), invoice_number="134-052-1")
        make_invoice(supplier=metro, invoice_date=date(2026, 9, 10), invoice_number="T-1", ocr_text="METRO ticket")
        make_invoice(supplier=metro, invoice_date=date(2099, 1, 1), invoice_number="134-052-2")
        _job, scrape_metro, *_ = self.gather(lambda *a, **k: [], start=date(2026, 9, 15))
        self.assertLessEqual(scrape_metro.call_args.args[1], date(2026, 6, 1))

    def test_cancelling_reaches_metro(self):
        job, scrape_metro, *_ = self.gather(lambda *a, **k: [])
        should_cancel = scrape_metro.call_args.kwargs["should_cancel"]
        self.assertFalse(should_cancel())
        ScrapeJob.objects.filter(pk=job.pk).update(cancel_requested=True)
        self.assertTrue(should_cancel())


class MailboxInTheGatherTests(TestCase):
    def test_a_mailbox_failing_is_said_on_its_line_and_the_others_go_on(self):
        """A revoked app password failed the whole gather: the other types
        and the five portals were never searched."""
        import imaplib

        first = email_type(make_supplier(code="SALLE_X", name="Salle Exemple", parser_key=""), "Salle Exemple")
        second = email_type(make_supplier(code="GROS_Y", name="Grossiste Y", parser_key=""), "Grossiste Y")
        def mailbox(download_dir, *args, **kwargs):
            if download_dir.endswith(f"type-{first.id}"):
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            return []

        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_email_invoices", side_effect=mailbox) as scrape_email:
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), {f"type-{first.id}", f"type-{second.id}"})
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        self.assertEqual(scrape_email.call_count, 2)
        self.assertIn("AUTHENTICATIONFAILED", job.progress[f"type-{first.id}"]["error"])
        self.assertNotIn("error", job.progress[f"type-{second.id}"])


class HeartbeatTests(TestCase):
    """A gather silent for long - an OCR, a sleeping laptop - was reaped
    while its thread ran on, and a second one could be started."""

    def test_a_beat_cannot_make_an_import_fail_on_a_locked_database(self):
        """A transaction that has read, then writes, after another connection
        committed a write (a beat) fails at once with "database is locked" in
        SQLite's default mode - the busy timeout is not even tried - and the
        invoice being imported was lost. Taking the write lock at the start
        of the transaction waits instead (Django's transaction_mode)."""
        from config import settings as real_settings

        self.assertEqual(real_settings.SQLITE_OPTIONS.get("transaction_mode"), "IMMEDIATE")

    def test_a_gather_stuck_in_one_call_stops_being_said_alive(self):
        """Beating whatever the gather did, a thread blocked for good in one
        call was never reaped: every new gather refused until a restart."""
        from invoices.tasks import _GatherHeartbeat
        from common import JobLogMixin

        job = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, log="[+   1.0s] Metro : connexion…")
        now = [0.0]
        heartbeat = _GatherHeartbeat(job.id, clock=lambda: now[0])
        heartbeat.beat()
        ScrapeJob.objects.filter(pk=job.pk).update(last_heartbeat=None)
        now[0] = JobLogMixin.STALE_AFTER.total_seconds() + 1
        heartbeat.beat()
        job.refresh_from_db()
        self.assertIsNone(job.last_heartbeat, "a gather making no progress was still said alive")
        # Progress again: alive again (the log line itself also beats: cleared).
        job.append_log("Metro : fenêtre suivante")
        ScrapeJob.objects.filter(pk=job.pk).update(last_heartbeat=None)
        heartbeat.beat()
        job.refresh_from_db()
        self.assertIsNotNone(job.last_heartbeat)

    def test_a_beat_says_alive_and_undoes_a_reaping(self):
        from invoices.tasks import _GatherHeartbeat

        job = ScrapeJob.objects.create(status=ScrapeJob.Status.FAILED)
        _GatherHeartbeat(job.id).beat()
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.RUNNING)
        self.assertIsNotNone(job.last_heartbeat)

    def test_the_gather_beats_while_it_runs(self):
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks._GatherHeartbeat") as heartbeat:
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), set())
        heartbeat.return_value.start.assert_called_once()
        heartbeat.return_value.stop.assert_called_once()


class EmailImportTests(TestCase):
    def gather(self, invoice_type, files):
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_email_invoices", return_value=files), mock.patch(
            "invoices.receipts.import_document"
        ) as import_document, mock.patch("invoices.tasks.parse_and_import") as parse_and_import:
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), {f"type-{invoice_type.id}"})
        job.refresh_from_db()
        return job, import_document, parse_and_import

    def test_a_supplier_without_a_reader_is_read_not_filed_empty(self):
        """Filed empty - no lines, no number - and again at every gather:
        the duplicate check is on the number, and there was none."""
        venue = make_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        invoice_type = email_type(venue, "Salle Exemple - Factures")
        job, import_document, parse_and_import = self.gather(invoice_type, [("/tmp/facture-12.pdf", date(2026, 5, 2))])
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        parse_and_import.assert_not_called()
        self.assertEqual(import_document.call_args.kwargs["supplier"], venue)
        self.assertEqual(import_document.call_args.kwargs["date_hint"], date(2026, 5, 2))
        self.assertEqual(job.progress[f"type-{invoice_type.id}"]["imported"], 1)

    def test_a_type_naming_its_reader_keeps_it(self):
        wholesaler = make_supplier(code="GROS_X", name="Grossiste Exemple", parser_key="")
        invoice_type = email_type(wholesaler, "Grossiste - Factures", parser_key="UBA")
        _job, import_document, parse_and_import = self.gather(invoice_type, [("/tmp/f.pdf", date(2026, 5, 2))])
        import_document.assert_not_called()
        self.assertEqual(parse_and_import.call_args.kwargs["parser_key_override"], "UBA")

    def test_a_file_already_imported_is_skipped_not_failed(self):
        from invoices.importing import DuplicateInvoiceError

        venue = make_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        invoice_type = email_type(venue, "Salle Exemple - Factures")
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_email_invoices", return_value=[("/tmp/f.pdf", None)]), mock.patch(
            "invoices.receipts.import_document", side_effect=DuplicateInvoiceError("Fichier déjà importé")
        ):
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), {f"type-{invoice_type.id}"})
        job.refresh_from_db()
        self.assertEqual((job.status, job.invoices_created), (ScrapeJob.Status.SUCCESS, 0))
        self.assertIn("already imported", job.log)

    def test_the_emails_date_reaches_a_suppliers_own_reader(self):
        """A reader reading no date falls back on the email's: dropped on
        the way, the invoice was filed undated."""
        import tempfile

        from invoices.receipts import import_document

        wholesaler = make_supplier(code="GROS_X", name="Grossiste Exemple", parser_key="UBA")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(b"%PDF-1.4 facture exemple")
        self.addCleanup(os.remove, handle.name)
        invoice = mock.Mock(source_text="")
        with mock.patch("invoices.receipts.document_text", return_value="Grossiste Exemple"), mock.patch(
            "invoices.receipts.has_own_reader", return_value=True
        ), mock.patch("invoices.receipts.learn_identifiers"), mock.patch(
            "invoices.receipts._record_first_document"
        ), mock.patch(
            "invoices.importing.parse_and_import", return_value=invoice
        ) as parse_and_import:
            import_document(handle.name, supplier=wholesaler, date_hint=date(2026, 5, 2))
        self.assertEqual(parse_and_import.call_args.kwargs["date_hint"], date(2026, 5, 2))

    def test_the_download_folder_is_built_whatever_the_setting_is(self):
        """A str in the test settings, a Path in the real ones: "/" on a str
        raised, and no test could run the mailbox or Metro part."""
        venue = make_supplier(code="SALLE_X", name="Salle Exemple", parser_key="")
        invoice_type = email_type(venue, "Salle Exemple - Factures")
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]) as scrape:
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), {f"type-{invoice_type.id}"})
        self.assertTrue(scrape.call_args.args[0].endswith(os.path.join("", f"type-{invoice_type.id}")))
