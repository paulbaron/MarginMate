"""« Récupérer » for the slips: the mailbox gather's returnables step.

The mailbox is never contacted: `invoices.tasks.find_matching_emails` is
replaced and hands back invented mails (EmailMatch), whose attachments are
tiny PDFs printing an invented slip. What is tested is what the gather does
with them - where it searches from, what it stores, what it says on its line
- and that a slip never becomes an invoice. Every value invented.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.models import EmailInvoiceSource, Invoice, InvoiceType, ScrapeJob
from invoices.scrapers.generic_email import EmailAttachment, EmailMatch
from invoices.tasks import gather_invoices_task
from returnables import comparison, mail, patterns, reading, slips
from returnables.models import Slip
from returnables.tests.support import (
    DELIVERY_DAY,
    KEG_LINE,
    SEEDED_FORMAT_NAME,
    make_format,
    make_pickup,
    make_slip,
    make_supplier,
    seeded_format,
    slip_text,
    tiny_pdf,
)
from returnables.tests.test_patterns import NeverCompile
from tests.support import NoNetworkTestCase, _Forbidden

#: What UBA's driver's mails look like (the seeded format's patterns read
#: them); the tour, the account and the day are invented.
SLIP_SENDER = "mphone@uba.paris"
SLIP_SUBJECT = "Livraison du 10/02/2026 Tour. : ZZZ Compte : 00000"
LABEL = f"Bons de consignes — {SEEDED_FORMAT_NAME}"


def slip_pdf(number="4243", references=("555001",), lines=(KEG_LINE,), copy=""):
    """An invented slip, as a PDF (`copy`: other bytes, same reading)."""
    text = slip_text(number=number, references=references, lines=lines)
    return tiny_pdf(text.split("\n") + ([copy] if copy else []))


def mail_of(*contents, name="T0000000001.pdf", day=DELIVERY_DAY, sender=SLIP_SENDER, subject=SLIP_SUBJECT):
    return EmailMatch(
        message_id=b"1",
        sender=sender,
        subject=subject,
        email_date=day,
        attachments=[EmailAttachment(filename=name, content=content) for content in contents],
    )


def code_of(fmt) -> str:
    return f"bons-{fmt.pk}"


class _Logged(list):
    def __call__(self, line):
        self.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self)


class FetchStartTests(NoNetworkTestCase):
    """Where a format's search starts. Only slips brought in BY MAIL move it:
    a hand upload says nothing about which mails were read."""

    def setUp(self):
        super().setUp()
        self.fmt = seeded_format()
        self.today = date(2026, 9, 20)

    def test_with_no_mailed_slip_it_looks_ninety_days_back(self):
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2026, 6, 22))

    def test_a_posted_start_earlier_wins(self):
        self.assertEqual(mail.fetch_start(self.fmt, date(2026, 3, 1), self.today), date(2026, 3, 1))

    def test_a_posted_start_later_does_not_skip_the_mails_since_the_newest_slip(self):
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 9, 1))
        self.assertEqual(mail.fetch_start(self.fmt, date(2026, 9, 15), self.today), date(2026, 8, 29))

    def test_from_a_few_days_before_the_newest_mailed_slip(self):
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 8, 1))
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 9, 10))
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2026, 9, 7))

    def test_a_slip_dropped_by_hand_never_moves_it(self):
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 8, 1))
        make_slip(origin=Slip.Origin.UPLOAD, mail_date=date(2026, 9, 18))
        make_slip(origin=Slip.Origin.UPLOAD, delivery_date=date(2026, 9, 18))
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2026, 7, 29))

    def test_a_mail_dated_in_the_future_is_left_out(self):
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 8, 1))
        make_slip(origin=Slip.Origin.MAIL, mail_date=date(2026, 12, 25))
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2026, 7, 29))

    def test_another_formats_slips_say_nothing(self):
        other = make_format(section_start="^AUTRE BON$", supplier=make_supplier())
        make_slip(other, origin=Slip.Origin.MAIL, mail_date=date(2026, 9, 18))
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2026, 6, 22))

    def test_never_further_back_than_four_hundred_days(self):
        self.assertEqual(mail.fetch_start(self.fmt, date(2020, 1, 1), self.today), date(2025, 8, 16))
        self.assertEqual(mail.fetch_start(self.fmt, None, self.today) - self.today, timedelta(days=-90))

    def test_a_mail_dated_at_the_calendar_s_start_never_overflows(self):
        """A mail date a damaged archive brought in before « Données »
        refused it: the home page asks for this start at every drawing, so
        « 3 days before » it was a 500 on /consignes/ for good."""
        for mail_date in (date(1, 1, 1), date(1, 1, 3)):
            with self.subTest(mail_date=mail_date):
                make_slip(origin=Slip.Origin.MAIL, mail_date=mail_date)
                self.assertEqual(mail.fetch_start(self.fmt, None, self.today), date(2025, 8, 16))
                self.assertEqual(mail.fetch_start(self.fmt, date(2026, 9, 1), self.today), date(2025, 8, 16))
                Slip.objects.all().delete()


class StoreMatchesTests(NoNetworkTestCase):
    """What becomes of the mails found: every attachment through the one
    writer, « Reçu par mail », and every outcome a log line."""

    def setUp(self):
        super().setUp()
        self.fmt = seeded_format()
        self.log = _Logged()
        # A slip is no invoice: nothing here may reach an invoice import.
        for target in (
            "invoices.receipts.import_document",
            "invoices.importing.parse_and_import",
            "invoices.importing.import_parsed_invoice",
        ):
            self.enterContext(mock.patch(target, side_effect=AssertionError(f"{target} called for a bon")))

    def store(self, *matches, **kwargs):
        return mail.store_matches(self.fmt, list(matches), self.log, **kwargs)

    def test_a_new_slip_is_stored_with_its_mail(self):
        found, imported, note = self.store(mail_of(slip_pdf()))
        self.assertEqual((found, imported, note), (1, 1, ""))
        slip = Slip.objects.get()
        self.assertEqual(
            (slip.origin, slip.format, slip.mail_sender, slip.mail_subject, slip.mail_date, slip.original_name),
            (Slip.Origin.MAIL, self.fmt, SLIP_SENDER, SLIP_SUBJECT, DELIVERY_DAY, "T0000000001.pdf"),
        )
        self.assertEqual((slip.number, slip.references, slip.delivery_date), ("4243", ["555001"], DELIVERY_DAY))
        self.assertIn("T0000000001.pdf : bon n° 4243 du 10/02/2026 ajouté.", self.log)
        self.assertFalse(Invoice.objects.exists())

    def test_the_same_bytes_again_are_a_log_line(self):
        content = slip_pdf()
        self.store(mail_of(content))
        found, imported, _note = self.store(mail_of(content))
        self.assertEqual((found, imported, Slip.objects.count()), (1, 0, 1))
        self.assertIn("déjà reçu", self.log[-1])

    def test_a_resend_is_a_log_line_too(self):
        """The driver mails a slip twice, printed again the next morning."""
        self.store(mail_of(slip_pdf()))
        found, imported, _note = self.store(mail_of(slip_pdf(copy="reimpression")))
        self.assertEqual((found, imported, Slip.objects.count()), (1, 0, 1))
        self.assertIn("ce document en est un renvoi", self.log[-1])

    def test_a_mails_other_attachment_is_ignored_not_stored(self):
        terms = tiny_pdf(["CONDITIONS GENERALES DE VENTE", "Article 1 : exemple"])
        found, imported, _note = self.store(mail_of(terms, slip_pdf(), name="cgv.pdf"))
        self.assertEqual((found, imported, Slip.objects.count()), (1, 1, 1))
        self.assertIn(f"cgv.pdf : pas un bon « {SEEDED_FORMAT_NAME} » — ignoré.", self.log)

    def test_an_attachment_over_the_limit_is_ignored_unread(self):
        with mock.patch.object(reading, "MAX_PDF_BYTES", 50), mock.patch.object(slips, "store_slip") as store:
            found, imported, _note = self.store(mail_of(slip_pdf(), name="lourd.pdf"))
        store.assert_not_called()
        self.assertEqual((found, imported), (0, 0))
        self.assertIn(f"lourd.pdf : pièce jointe ignorée — {reading.TOO_HEAVY}", self.log)

    def test_the_note_is_the_latest_pickups_comparison(self):
        make_pickup(counts={"Fûts": 3})
        _found, _imported, note = self.store(mail_of(slip_pdf()))
        self.assertTrue(note.startswith("Reprise du 10/02/2026"), note)
        self.assertEqual(note, comparison.latest_note())

    def test_a_note_that_cannot_be_made_is_no_failure(self):
        with (
            mock.patch.object(comparison, "latest_note", side_effect=RuntimeError("inattendu")),
            self.assertLogs("returnables.mail", "ERROR"),
        ):
            found, imported, note = self.store(mail_of(slip_pdf()))
        self.assertEqual((found, imported, note), (1, 1, ""))
        self.assertIn("la dernière reprise n'a pas pu être comparée", self.log.text)

    def test_the_mails_are_stored_oldest_first(self):
        """Stopped by an error, a run has stored the earliest: the next run's
        start - the newest stored - skips none of the rest."""
        late, early = mail_of(b"%PDF-B", day=date(2026, 9, 12)), mail_of(b"%PDF-A", day=date(2026, 9, 2))
        undated = mail_of(b"%PDF-C", day=None)
        with mock.patch.object(
            slips,
            "store_slip",
            side_effect=[
                slips.StoreResult(None, False, slips.REFUSED, "x"),
                RuntimeError("base verrouillée"),
            ],
        ) as store:
            with self.assertRaises(RuntimeError):
                self.store(late, undated, early)
        self.assertEqual([call.args[0] for call in store.call_args_list], [b"%PDF-A", b"%PDF-B"])

    def test_progress_is_told_of_each_new_slip(self):
        told = []
        self.store(mail_of(slip_pdf("11"), slip_pdf("12", references=("555002",))), progress=lambda *a: told.append(a))
        self.assertEqual(told, [(1, 1), (2, 2)])


class GatherStepTests(NoNetworkTestCase):
    """The step inside gather_invoices_task: gated by its code, contained on
    its line, kept out of the invoice totals."""

    def setUp(self):
        super().setUp()
        self.fmt = seeded_format()
        self.code = code_of(self.fmt)
        self.enterContext(mock.patch("invoices.tasks._GatherHeartbeat"))

    def gather(self, codes, found=(), *, job=None, start=date(2026, 9, 1), **patches):
        job = job or ScrapeJob.objects.create()
        search = patches.pop("search", None)
        with (
            mock.patch("invoices.tasks.find_matching_emails", side_effect=search, return_value=list(found)) as find,
            mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]),
            mock.patch("invoices.tasks.parse_and_import", side_effect=AssertionError("an invoice import for a bon")),
            mock.patch("invoices.receipts.import_document", side_effect=AssertionError("an invoice import for a bon")),
        ):
            gather_invoices_task(job.id, start, date(2026, 9, 20), codes)
        job.refresh_from_db()
        return job, find

    def test_the_slips_are_searched_and_stored_on_their_own_line(self):
        job, find = self.gather({self.code}, [mail_of(slip_pdf())])
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        find.assert_called_once()
        kwargs = find.call_args.kwargs
        self.assertEqual(
            (kwargs["sender_pattern"], kwargs["subject_pattern"], kwargs["attachment_pattern"]),
            (self.fmt.sender_pattern, self.fmt.subject_pattern, self.fmt.attachment_pattern),
        )
        self.assertIs(kwargs["compile"].func, patterns.mail_matcher)
        self.assertEqual(job.progress[self.code], {"label": LABEL, "found": 1, "imported": 1})
        self.assertEqual(Slip.objects.get().origin, Slip.Origin.MAIL)

    def test_slips_stay_out_of_the_invoice_totals(self):
        job, _find = self.gather({self.code}, [mail_of(slip_pdf())])
        self.assertEqual((job.invoices_found, job.invoices_created), (0, 0))
        self.assertFalse(Invoice.objects.exists())

    def test_the_patterns_go_through_the_returnables_guard(self):
        """Case-insensitive and timed: the seeded sender reads a sender
        written in capitals, and the timeout's log goes to the job."""
        _job, find = self.gather({self.code})
        matcher = find.call_args.kwargs["compile"](self.fmt.sender_pattern)
        self.assertIsNotNone(matcher.search("MPHONE@UBA.PARIS"))
        self.assertIs(find.call_args.kwargs["compile"].keywords["log"].__self__.pk, _job.pk)

    def test_nothing_named_runs_nothing(self):
        for codes in (set(), {"type-999"}):
            with self.subTest(codes=codes):
                _job, find = self.gather(codes)
                find.assert_not_called()

    def test_a_gather_of_every_source_includes_the_slips(self):
        """None (a shell, a script) is every eligible source but Metro."""
        _job, find = self.gather(None)
        find.assert_called_once()
        self.assertEqual(find.call_args.kwargs["sender_pattern"], self.fmt.sender_pattern)

    def test_only_active_formats_fetched_by_mail_take_part(self):
        no_sender = make_format(name="Déposé seulement", supplier=make_supplier(), sender_pattern="")
        inactive = make_format(name="Ancien format", supplier=make_supplier(), is_active=False)
        job, find = self.gather({self.code, code_of(no_sender), code_of(inactive)})
        find.assert_called_once()
        self.assertEqual(set(job.progress), {self.code})

    def test_a_failure_stays_on_its_line_and_the_others_go_on(self):
        import imaplib

        other = make_format(
            name="Autre livreur",
            supplier=make_supplier(),
            sender_pattern=r"livreur@exemple\.invalid",
            section_start="^AUTRE BON$",
        )

        def search(start, end, sender_pattern, **kwargs):
            if sender_pattern == other.sender_pattern:
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            return [mail_of(slip_pdf())]

        job, find = self.gather({self.code, code_of(other)}, search=search)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        self.assertEqual(find.call_count, 2)
        self.assertIn("AUTHENTICATIONFAILED", job.progress[code_of(other)]["error"])
        self.assertNotIn("error", job.progress[self.code])
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(len(job.failed_sources), 1)

    @override_settings(INVOICE_EMAIL_ADDRESS="factures@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="secret-essai")
    def test_a_pattern_the_guard_refuses_is_the_formats_error_and_nothing_signs_in(self):
        """A stored pattern never goes to a compiler unchecked (29/09: a
        counted repetition compiled for real froze the owner's PC). The real
        search runs; the mailbox client may not be touched."""
        import regex

        type(self.fmt).objects.filter(pk=self.fmt.pk).update(sender_pattern=r"x{e<=1}@exemple\.invalid")
        never = NeverCompile()
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        job = ScrapeJob.objects.create()
        from invoices.scrapers import generic_email

        with (
            mock.patch.object(regex, "compile", new=never),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL", new=_Forbidden("IMAP")),
            mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]),
        ):
            with mock.patch("invoices.tasks.find_matching_emails", new=generic_email.find_matching_emails):
                gather_invoices_task(job.id, date(2026, 9, 1), date(2026, 9, 20), {self.code})
        job.refresh_from_db()
        self.assertEqual(never.calls, [])
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        error = job.progress[self.code]["error"]
        self.assertTrue(error.startswith("Motif de mail : "), error)
        self.assertIn("accolade", error)
        self.assertNotIn("real IMAP connection", job.log)

    def test_a_duplicate_is_a_log_line_never_an_error(self):
        self.gather({self.code}, [mail_of(slip_pdf())])
        job, _find = self.gather({self.code}, [mail_of(slip_pdf())])
        self.assertEqual(job.progress[self.code], {"label": LABEL, "found": 1, "imported": 0})
        self.assertIn("déjà reçu", job.log)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(job.failed_sources, [])

    def test_the_comparison_is_a_note_never_an_error(self):
        make_pickup(counts={"Fûts": 3})
        job, _find = self.gather({self.code}, [mail_of(slip_pdf())])
        entry = job.progress[self.code]
        self.assertNotIn("error", entry)
        self.assertEqual(entry["note"], comparison.latest_note())
        self.assertEqual(job.failed_sources, [])

    def test_the_search_starts_from_the_newest_mailed_slip_and_widens_the_range(self):
        today = timezone.localdate()
        make_slip(origin=Slip.Origin.MAIL, mail_date=today - timedelta(days=10))
        make_slip(origin=Slip.Origin.UPLOAD, mail_date=today - timedelta(days=1))
        posted = today - timedelta(days=5)
        job = ScrapeJob.objects.create(range_start=posted, range_end=today)
        job, find = self.gather({self.code}, job=job, start=posted)
        self.assertEqual(find.call_args.args[0], today - timedelta(days=13))
        self.assertEqual(job.range_start, today - timedelta(days=13))

    def test_what_was_found_before_a_cancel_is_stored_then_the_run_stops(self):
        """The search stops early on a cancel and hands back what it had:
        dropped, the next run's start would have skipped those mails."""
        job = ScrapeJob.objects.create()

        def search(*args, **kwargs):
            ScrapeJob.objects.filter(pk=job.pk).update(cancel_requested=True)
            return [mail_of(slip_pdf())]

        job, _find = self.gather({self.code}, job=job, search=search)
        self.assertEqual(Slip.objects.count(), 1)
        self.assertEqual(job.status, ScrapeJob.Status.CANCELLED)

    def test_cancelling_reaches_the_search(self):
        job, find = self.gather({self.code})
        should_cancel = find.call_args.kwargs["should_cancel"]
        self.assertFalse(should_cancel())
        ScrapeJob.objects.filter(pk=job.pk).update(cancel_requested=True)
        self.assertTrue(should_cancel())

    def test_a_tenant_without_the_returnables_tables_still_gathers_its_invoices(self):
        from django.db import OperationalError

        with mock.patch("returnables.models.SlipFormat.objects.filter", side_effect=OperationalError("no such table")):
            job, find = self.gather({self.code})
        find.assert_not_called()
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        self.assertIn("les formats de bon n'ont pas pu être lus", job.log)


class SeededSourcesTests(NoNetworkTestCase):
    """The seeded invoice source for UBA (invoices 0007) must not take the
    driver's slips for invoices - its patterns are run by `re`, as the gather
    runs them - and the seeded format takes them."""

    def test_the_uba_invoice_source_does_not_match_a_slip_mail(self):
        source = EmailInvoiceSource.objects.get(invoice_type__supplier__code="UBA")
        self.assertIsNone(re.search(source.sender_pattern, SLIP_SENDER))
        self.assertIsNone(re.search(source.subject_pattern, SLIP_SUBJECT))

    def test_the_seeded_format_does(self):
        fmt = seeded_format()
        for pattern, text in (
            (fmt.sender_pattern, SLIP_SENDER),
            (fmt.subject_pattern, SLIP_SUBJECT),
            (fmt.attachment_pattern, "T0000000001.pdf"),
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(patterns.mail_matcher(pattern).search(text))


class GatherCardTests(NoNetworkTestCase):
    """Achats' « Récupérer » card offers each format fetched by mail, ticked
    like any source; its status card counts slips as slips."""

    def card(self):
        return self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")

    def test_each_format_fetched_by_mail_is_a_source(self):
        fmt = seeded_format()
        make_format(name="Déposé seulement", supplier=make_supplier(), sender_pattern="")
        make_format(name="Ancien format", supplier=make_supplier(), is_active=False, sender_pattern="a@b")
        page = self.card()
        slip_sources = [source for source in page.context["gather_sources"] if source["code"].startswith("bons-")]
        self.assertEqual(slip_sources, [{"code": code_of(fmt), "label": LABEL}])
        self.assertContains(page, f'value="{code_of(fmt)}" checked')
        self.assertContains(page, f'data-persist="source:{code_of(fmt)}"')

    def test_a_job_of_slips_only_counts_slips(self):
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.SUCCESS, progress={"bons-1": {"label": LABEL, "found": 2, "imported": 1}}
        )
        card = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertContains(card, "Bons trouvés")
        self.assertContains(card, "Nouveaux")
        self.assertNotContains(card, "Trouvées")
        self.assertNotContains(card, "Importées")

    def test_a_gather_of_invoices_says_it_as_before(self):
        job = ScrapeJob.objects.create(
            status=ScrapeJob.Status.SUCCESS,
            progress={
                "type-1": {"label": "Grossiste", "found": 2, "imported": 2},
                "bons-1": {"label": LABEL, "found": 1, "imported": 1},
            },
        )
        card = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertContains(card, "Trouvées")
        self.assertContains(card, "Importées")
        self.assertNotContains(card, "Bons trouvés")


class SlipsOnlyTests(NoNetworkTestCase):
    def test_every_source_a_format(self):
        cases = (
            ({"bons-1": {}}, True),
            ({"bons-1": {}, "bons-2": {}}, True),
            ({"bons-1": {}, "type-3": {}}, False),
            ({"METRO": {}}, False),
            ({}, False),
        )
        for progress, expected in cases:
            with self.subTest(progress=progress):
                self.assertIs(ScrapeJob(progress=progress).slips_only, expected)


class PurchasesPeriodTests(NoNetworkTestCase):
    """Achats offers a gather's period again when it missed something - never
    a gather of slips only: its period is the slips' own start (90 days back
    on a first run), and the invoices' default start went back with it."""

    def start_shown(self):
        return self.client.get(reverse("invoices:invoice_list")).context["default_start_date"]

    def gather_job(self, status, progress, start=date(2026, 1, 1), **fields):
        return ScrapeJob.objects.create(
            status=status, range_start=start, range_end=date(2026, 9, 18), progress=progress, **fields
        )

    def test_a_failed_run_of_slips_only_is_not_offered_again(self):
        self.gather_job(ScrapeJob.Status.SUCCESS, {"bons-1": {"label": LABEL, "error": "Boîte mail : refusée"}})
        self.assertNotEqual(self.start_shown(), date(2026, 1, 1))
        self.gather_job(ScrapeJob.Status.FAILED, {"bons-1": {"label": LABEL}})
        self.assertNotEqual(self.start_shown(), date(2026, 1, 1))

    def test_a_running_one_is_not_either(self):
        self.gather_job(ScrapeJob.Status.RUNNING, {"bons-1": {"label": LABEL}}, last_heartbeat=timezone.now())
        self.assertNotEqual(self.start_shown(), date(2026, 1, 1))

    def test_the_invoice_gather_before_it_still_is(self):
        self.gather_job(
            ScrapeJob.Status.SUCCESS, {"METRO": {"label": "Metro", "error": "Metro a bloqué."}}, start=date(2026, 2, 1)
        )
        self.gather_job(ScrapeJob.Status.FAILED, {"bons-1": {"label": LABEL}})
        self.assertEqual(self.start_shown(), date(2026, 2, 1))

    def test_a_run_of_invoices_and_slips_is_an_invoice_gather(self):
        self.gather_job(
            ScrapeJob.Status.SUCCESS,
            {
                "type-1": {"label": "Grossiste"},
                "bons-1": {"label": LABEL, "error": "Boîte mail : refusée"},
            },
        )
        self.assertEqual(self.start_shown(), date(2026, 1, 1))

    def test_slips_only_runs_between_do_not_hide_a_source_failing_twice(self):
        """_missed_again compares an invoice gather with the invoice gather
        before it: a slips-only run in between used to be « the previous »."""
        failing = {"type-5": {"label": "Portail", "error": "Code SMS demandé."}}
        self.gather_job(ScrapeJob.Status.SUCCESS, failing)
        self.gather_job(ScrapeJob.Status.SUCCESS, {"bons-1": {"label": LABEL}})
        self.gather_job(ScrapeJob.Status.SUCCESS, failing)
        self.assertNotEqual(self.start_shown(), date(2026, 1, 1))


class GatherTenantsTests(TwoTenantsTestCase):
    """Bar Alpha is the platform owner's espace; Bar Beta another bar, whose
    slips come through its own mailbox (its « Identifiants »). Both have
    UBA's format, under the same pk: Beta, a new hosted espace, starts
    without it (invoices.seeds) and is given it here."""

    owner_a = True

    def setUp(self):
        super().setUp()
        for target, label in (("imaplib.IMAP4_SSL", "IMAP"), ("smtplib.SMTP", "SMTP"), ("smtplib.SMTP_SSL", "SMTP")):
            patcher = mock.patch(target, new=_Forbidden(label))
            patcher.start()
            self.addCleanup(patcher.stop)
        with bound_tenant(self.bar_a):
            seeded = seeded_format()
            self.code = code_of(seeded)
        with bound_tenant(self.bar_b):
            make_format(name=SEEDED_FORMAT_NAME, pk=seeded.pk)
            self.assertEqual(code_of(seeded_format()), self.code, "the same pk in both, or this proves less")

    def beta_s_mailbox(self):
        """Beta's mailbox typed on its « Identifiants » page."""
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save(
                {"INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": "secret-beta"},
                bindings={"INVOICE_EMAIL_APP_PASSWORD": "imap.beta.invalid"},
            )

    def test_the_card_offers_the_slips_in_every_espace_with_its_mailbox(self):
        self.client.force_login(self.user_a)
        self.assertContains(
            self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer"), f'value="{self.code}"'
        )
        self.beta_s_mailbox()
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertContains(page, f'value="{self.code}"')
        self.assertNotContains(page, integrations.GATHER)

    def test_a_gather_of_slips_in_beta_runs_bound_to_beta(self):
        self.client.force_login(self.user_b)
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(reverse("invoices:gather"), {"sources": [self.code], "retour": "/consignes/"})
        self.assertEqual(response["Location"], "/consignes/")
        target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        self.assertEqual(args[3], {self.code})
        with bound_tenant(self.bar_a):
            self.assertFalse(ScrapeJob.objects.exists())

    def test_alphas_gather_of_slips_runs_bound_to_alpha(self):
        self.client.force_login(self.user_a)
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(
                reverse("invoices:gather"),
                {
                    "sources": [self.code],
                    "start_date": "2026-06-01",
                    "end_date": "2026-09-20",
                    "retour": "/consignes/",
                },
            )
        self.assertEqual(response["Location"], "/consignes/")
        target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertIs(target.__wrapped__, gather_invoices_task)
        self.assertEqual(target.tenant.pk, self.bar_a.pk)
        self.assertEqual(args[3], {self.code})

    def test_the_task_refuses_by_itself_unbound(self):
        job = ScrapeJob.objects.create()
        with (
            mock.patch("invoices.tasks.find_matching_emails") as find,
            mock.patch("invoices.tasks._GatherHeartbeat"),
        ):
            gather_invoices_task(job.pk, date(2026, 9, 1), date(2026, 9, 20), {self.code})
        job.refresh_from_db()
        find.assert_not_called()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        self.assertIn(integrations.GATHER, job.log)
        with bound_tenant(self.bar_b):
            self.assertFalse(Slip.objects.exists())

    def test_alphas_slips_land_in_alpha(self):
        with bound_tenant(self.bar_a):
            job = ScrapeJob.objects.create()
            with (
                mock.patch("invoices.tasks.find_matching_emails", return_value=[mail_of(slip_pdf())]),
                mock.patch("invoices.tasks._GatherHeartbeat"),
            ):
                gather_invoices_task(job.pk, date(2026, 9, 1), date(2026, 9, 20), {self.code})
            self.assertEqual(Slip.objects.count(), 1)
        with bound_tenant(self.bar_b):
            self.assertFalse(Slip.objects.exists())


class NoInvoiceTypeRowTests(NoNetworkTestCase):
    """The slips are fetched as code over a format, never as an invoice
    source: a row would be picked up by the invoice mailbox loop, counted on
    « Sources », and missed by switch_off_server_integrations."""

    def test_no_invoice_source_is_made_for_the_slips(self):
        before = InvoiceType.objects.count()
        with (
            mock.patch("invoices.tasks.find_matching_emails", return_value=[mail_of(slip_pdf())]),
            mock.patch("invoices.tasks._GatherHeartbeat"),
        ):
            job = ScrapeJob.objects.create()
            gather_invoices_task(job.pk, date(2026, 9, 1), date(2026, 9, 20), {code_of(seeded_format())})
        self.assertEqual(InvoiceType.objects.count(), before)


class SlipsNeverWidenPurchasesTests(NoNetworkTestCase):
    """The slips search from their own start (90 days back while no slip has
    come by mail), but only a gather of slips ALONE shows that start as its
    period. Ticked on Achats beside the invoices, they widened the job's
    range: a portal failing then had Achats offer 90 days back to every
    source, Metro included, where the invoices had asked for three."""

    def setUp(self):
        super().setUp()
        self.fmt = seeded_format()
        self.code = code_of(self.fmt)
        self.today = timezone.localdate()
        self.asked = self.today - timedelta(days=3)
        self.invoice_type = EmailInvoiceSource.objects.get(invoice_type__supplier__code="UBA").invoice_type
        self.type_code = f"type-{self.invoice_type.pk}"
        self.enterContext(mock.patch("invoices.tasks._GatherHeartbeat"))

    def gather(self, codes, mailbox=None, **fields):
        """As « Récupérer » runs it: the job made with the dates posted."""
        job = ScrapeJob.objects.create(range_start=self.asked, range_end=self.today, **fields)
        with (
            mock.patch("invoices.tasks.find_matching_emails", return_value=[]) as find,
            mock.patch("invoices.tasks.scrape_email_invoices", side_effect=mailbox, return_value=[]),
            mock.patch("invoices.tasks.parse_and_import", side_effect=AssertionError("an invoice import")),
            mock.patch("invoices.tasks._gather_website", return_value=(0, 0)),
        ):
            gather_invoices_task(job.pk, self.asked, self.today, codes)
        job.refresh_from_db()
        return job, find

    def start_shown(self):
        return self.client.get(reverse("invoices:invoice_list")).context["default_start_date"]

    def test_invoices_and_slips_keep_the_period_the_invoices_asked(self):
        import imaplib

        job, find = self.gather({self.type_code, self.code}, mailbox=imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS, job.log)
        self.assertIn("error", job.progress[self.type_code])
        # The slips still searched from their own start...
        self.assertEqual(find.call_args.args[0], self.today - timedelta(days=90))
        self.assertIn(f"{LABEL} : recherche depuis le {self.today - timedelta(days=90):%d/%m/%Y}.", job.log)
        # ... without taking the invoices' period with them.
        self.assertEqual(job.range_start, self.asked)
        self.assertEqual(self.start_shown(), self.asked)

    def test_every_source_from_a_shell_keeps_its_period_too(self):
        """None names every eligible source but Metro: not a gather of slips."""
        job, find = self.gather(None)
        find.assert_called_once()
        self.assertEqual(job.range_start, self.asked)

    def test_a_gather_of_slips_alone_still_shows_their_start(self):
        job, _find = self.gather({self.code})
        self.assertEqual(job.range_start, self.today - timedelta(days=90))

    def test_a_gather_of_slips_stopped_before_its_first_source_is_still_one(self):
        """Cancelled while it waited (or its thread killed at once), it had
        no source on its progress yet: it counted as a gather of invoices,
        and Achats offered the slips' start again."""
        returnables_start = self.today - timedelta(days=150)
        job = ScrapeJob.objects.create(range_start=returnables_start, range_end=self.today, cancel_requested=True)
        with mock.patch("invoices.tasks.find_matching_emails") as find:
            gather_invoices_task(job.pk, returnables_start, self.today, {self.code})
        job.refresh_from_db()
        find.assert_not_called()
        self.assertEqual(job.status, ScrapeJob.Status.CANCELLED)
        self.assertTrue(job.slips_only)
        self.assertEqual(job.progress, {self.code: {"label": LABEL, "found": 0, "imported": 0}})
        self.assertNotEqual(self.start_shown(), returnables_start)

    def test_a_format_the_run_does_not_name_is_not_named_on_it(self):
        other = make_format(name="Autre livreur", supplier=make_supplier(), sender_pattern=r"livreur@exemple\.invalid")
        job, find = self.gather({self.code})
        self.assertEqual(set(job.progress), {self.code})
        self.assertNotIn(code_of(other), job.progress)
        find.assert_called_once()
