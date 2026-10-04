"""Where each source of an automatic gather starts: the coverage each source
has been searched over WITHOUT a gap (invoices.GatherCoverage, invoices/
coverage.py), written when a source's search completes - by hand or not -
and never guessed from the day a gather ran (review findings R1-R4).

No gather ever runs for real: the task's mailbox, slips and portal calls are
replaced, the heartbeat too. Every name and date is invented.
"""

from datetime import date, time, timedelta
from unittest import mock

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import AutoGather, GatherCoverage, InvoiceType, ScrapeJob
from invoices.tasks import DEFAULT_LOOKBACK_DAYS, OVERLAP_DAYS, gather_invoices_task
from invoices.tests.test_auto_gather import WEDNESDAY, Sources, email_type
from invoices.tests.test_auto_gather_review import finished_gather
from invoices.tests.test_auto_gather_views import PAGE, PageCase, make_rule
from tests.factories import make_invoice, make_supplier


class Killed(BaseException):
    """The thread dying mid-search (a restart, a crash): nothing of the task
    after it runs - as `reap_stale` later finds the job."""


def catch_up_sentence(start: date, end: date) -> str:
    return f"Rattrapage à faire à la main depuis Factures, du {start:%d/%m/%Y} au {end:%d/%m/%Y}"


class CoverageCase(Sources, TestCase):
    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.today = timezone.localdate()
        self.rule = AutoGather.objects.create(
            name="Factures exemple",
            sources=[self.email_code, self.slips_code],
            weekdays=str(WEDNESDAY),
            start_time=time(7),
            end_time=time(7),
        )

    def days_ago(self, days) -> date:
        return self.today - timedelta(days=days)

    def gather(self, codes, *, unattended=True, start=None, end=None, email=None, slips=None):
        """One gather of `codes`; `email` / `slips` replace the mailbox's
        searches (their default finds nothing)."""
        job = ScrapeJob.objects.create(
            trigger=ScrapeJob.Trigger.AUTOMATIC if unattended else ScrapeJob.Trigger.MANUAL,
            auto_gather_id=self.rule.pk if unattended else None,
        )
        with (
            mock.patch("invoices.tasks.scrape_email_invoices", **(email or {"return_value": []})) as email_search,
            mock.patch("invoices.tasks.find_matching_emails", **(slips or {"return_value": []})) as slips_search,
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]),
            mock.patch("notifications.events.emit") as emit,
        ):
            gather_invoices_task(job.id, start, end, set(codes), False, unattended=unattended)
        job.refresh_from_db()
        return job, email_search, slips_search, emit

    def email_start(self, **kwargs) -> date:
        """Where the next automatic run searches the mailbox type from."""
        _job, email, *_ = self.gather({self.email_code}, **kwargs)
        return email.call_args.args[1]


class ManualGatherFromALaterStartTests(CoverageCase):
    """[R1] A manual gather searching from after a hole does not close it:
    the next automatic run searches the hole."""

    def outage(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(23))
        finished_gather(self.email_code, 22)
        for days in range(21, 0, -1):
            finished_gather(self.email_code, days, error=True, trigger=ScrapeJob.Trigger.AUTOMATIC)

    def test_a_gather_from_achats_default_start_leaves_the_hole_to_the_next_automatic_run(self):
        self.outage()
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(22 + OVERLAP_DAYS))

    def test_a_gather_of_a_past_period_does_not_move_it_either(self):
        self.outage()
        self.gather({self.email_code}, unattended=False, start=self.days_ago(60), end=self.days_ago(45))
        self.assertEqual(self.email_start(), self.days_ago(22 + OVERLAP_DAYS))

    def test_a_manual_gather_reaching_back_over_the_hole_closes_it(self):
        self.outage()
        self.gather({self.email_code}, unattended=False, start=self.days_ago(30))
        self.assertEqual(self.email_start(), self.days_ago(OVERLAP_DAYS))

    def test_the_automatic_run_that_searched_the_hole_closes_it(self):
        self.outage()
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.email_start()
        self.assertEqual(self.email_start(), self.days_ago(OVERLAP_DAYS))


class RunThatDidNotFinishTests(CoverageCase):
    """[R2] A source that raised, a cancelled run, a killed one record
    nothing: the next run starts where the last COMPLETED search did."""

    def setUp(self):
        super().setUp()
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(30))
        finished_gather(self.email_code, 25)

    def test_a_failed_run_whose_line_holds_no_error_is_no_search(self):
        # As reap_stale leaves a run killed mid-search.
        finished_gather(self.email_code, 1, status=ScrapeJob.Status.FAILED)
        self.assertEqual(self.email_start(), self.days_ago(25 + OVERLAP_DAYS))

    def test_a_run_killed_while_searching_records_nothing(self):
        with self.assertRaises(Killed):
            self.gather({self.email_code}, email={"side_effect": Killed()})
        ScrapeJob.objects.update(status=ScrapeJob.Status.FAILED)
        self.assertEqual(self.email_start(), self.days_ago(25 + OVERLAP_DAYS))

    def test_a_source_in_error_records_nothing(self):
        job, *_ = self.gather({self.email_code}, email={"side_effect": OSError("connexion perdue")})
        self.assertIn("error", job.progress[self.email_code])
        self.assertEqual(self.email_start(), self.days_ago(25 + OVERLAP_DAYS))

    def test_a_run_cancelled_during_the_search_records_nothing(self):
        def cancelled(*args, **kwargs):
            ScrapeJob.objects.update(cancel_requested=True)
            return []

        job, *_ = self.gather({self.email_code}, email={"side_effect": cancelled})
        self.assertEqual(job.status, ScrapeJob.Status.CANCELLED)
        self.assertEqual(self.email_start(), self.days_ago(25 + OVERLAP_DAYS))

    def test_a_run_failing_after_the_source_completed_keeps_what_it_searched(self):
        # The slip formats are looked through after the mailbox types.
        with mock.patch("invoices.tasks.slips_code", side_effect=RuntimeError("panne")):
            job, *_ = self.gather({self.email_code})
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        self.assertEqual(self.email_start(), self.days_ago(OVERLAP_DAYS))


class AnotherChannelTests(CoverageCase):
    """[R3] An invoice of the same supplier by another channel - a photo, a
    PDF dropped by hand - never jumps the mailbox's hole."""

    def setUp(self):
        super().setUp()
        finished_gather(self.email_code, 25)
        for days in range(24, 0, -1):
            finished_gather(self.email_code, days, error=True, trigger=ScrapeJob.Trigger.AUTOMATIC)

    def test_a_photographed_ticket_of_yesterday(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(1), ocr_text="Texte lu")
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(25 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_a_pdf_dropped_by_hand_yesterday(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(25 + OVERLAP_DAYS))


class StartTests(CoverageCase):
    def test_a_monthly_biller_searched_yesterday_starts_yesterday_less_the_overlap(self):
        from invoices.models import GatherCoverage

        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(30))
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(1 + OVERLAP_DAYS))

    def test_never_searched_starts_from_its_own_start(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        self.assertEqual(self.email_start(), self.days_ago(40 + OVERLAP_DAYS))

    def test_the_history_is_read_once_when_no_coverage_is_known(self):
        from invoices.models import GatherCoverage

        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        finished_gather(self.email_code, 10, trigger=ScrapeJob.Trigger.MANUAL)
        finished_gather(self.email_code, 5, error=True)
        self.assertEqual(self.email_start(), self.days_ago(10 + OVERLAP_DAYS))
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def test_a_coverage_known_wins_over_the_history(self):
        from invoices.models import GatherCoverage

        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(30))
        finished_gather(self.email_code, 1)
        self.assertEqual(self.email_start(), self.days_ago(30 + OVERLAP_DAYS))


class HistoryOfAPastPeriodTests(CoverageCase):
    """Read from the history, a gather of a past period covered its source up
    to the end it was asked for, not up to the day it ran."""

    def setUp(self):
        super().setUp()
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        finished_gather(self.email_code, 30)
        for days in range(29, 1, -1):
            finished_gather(self.email_code, days, error=True)
        # Yesterday, a past period gathered again by hand: up to 16 days ago.
        finished_gather(self.email_code, 1, trigger=ScrapeJob.Trigger.MANUAL, range_end=self.days_ago(16))

    def test_the_first_automatic_run_starts_where_that_period_ended(self):
        self.assertEqual(self.email_start(), self.days_ago(16 + OVERLAP_DAYS))

    def test_a_manual_gather_from_a_later_start_leaves_the_rest_to_the_next_automatic_run(self):
        self.gather({self.email_code}, unattended=False, start=self.days_ago(3))
        self.assertEqual(self.email_start(), self.days_ago(16 + OVERLAP_DAYS))


class PendingCatchUpTests(CoverageCase):
    """[R4] A stretch past the 90-day bound stays said - on every run, on the
    rule's card, in Achats' default start - until a search covers it."""

    def setUp(self):
        super().setUp()
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(200))
        self.cut = self.days_ago(200 + OVERLAP_DAYS)
        self.bound = self.days_ago(DEFAULT_LOOKBACK_DAYS)

    def test_said_on_every_run_until_a_search_covers_it(self):
        sentence = catch_up_sentence(self.cut, self.bound)
        for run in range(3):
            job, email, _slips, emit = self.gather({self.email_code})
            # The first run is cut at the bound; the next ones follow it.
            self.assertEqual(email.call_args.args[1], self.days_ago(OVERLAP_DAYS) if run else self.bound)
            self.assertEqual(job.progress[self.email_code]["catch_up"], sentence)
            self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))
            self.assertIn(sentence, emit.call_args.kwargs["body"])
            self.rule.refresh_from_db()
            self.assertEqual(self.rule.last_failed_codes, [self.email_code])

        # « Récupérer » with the dates Achats offers covers it.
        offered = self.achats_start()
        self.assertEqual(offered, self.cut)
        self.gather({self.email_code}, unattended=False, start=offered)
        job, _email, _slips, emit = self.gather({self.email_code})
        self.assertNotIn("catch_up", job.progress[self.email_code])
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "nothing"))
        self.assertEqual(self.achats_start(), self.days_ago(200))

    def test_a_manual_search_not_reaching_it_leaves_it_pending(self):
        self.gather({self.email_code})
        self.gather({self.email_code}, unattended=False, start=self.days_ago(100))
        job, *_ = self.gather({self.email_code})
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(self.cut, self.bound))

    def test_a_gather_by_hand_from_a_later_start_goes_on_offering_it(self):
        make_invoice(supplier=self.box.supplier, invoice_date=self.days_ago(1))
        self.gather({self.email_code})
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.assertEqual(self.achats_start(), self.cut)

    def test_a_source_failing_by_hand_holds_achats_back_no_more(self):
        # The default: another source's invoice of yesterday.
        make_invoice(supplier=self.box.supplier, invoice_date=self.days_ago(1))
        self.gather({self.email_code})
        self.assertEqual(self.achats_start(), self.cut)
        failing = {"side_effect": OSError("connexion perdue")}
        offered = []
        for _run in range(3):
            self.gather({self.email_code}, unattended=False, start=self.achats_start(), email=failing)
            offered.append(self.achats_start())
        # The failed period is offered once more (_missed_again), then never:
        # every source of every later « Récupérer » went back 200 days with it.
        self.assertEqual(offered, [self.cut, self.days_ago(1), self.days_ago(1)])
        # Still said on every automatic run's line.
        job, *_ = self.gather({self.email_code})
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(self.cut, self.bound))

    def test_a_source_left_unticked_holds_achats_back_no_more(self):
        beer = email_type(make_supplier(code="BIERE_X", name="Brasserie Exemple"), name="Brasserie Exemple - Factures")
        make_invoice(supplier=beer.supplier, invoice_date=self.days_ago(1))
        self.gather({self.email_code})
        self.assertEqual(self.achats_start(), self.cut)
        # « Récupérer » as offered, the Cave's box unticked: nothing searched it.
        self.gather({f"type-{beer.pk}"}, unattended=False, start=self.cut)
        self.assertEqual(self.achats_start(), self.days_ago(1))

    def test_a_chunk_of_the_stretch_moves_it_past_the_chunk(self):
        """[Q2] Caught up in chunks - a wide mailbox search reads thousands
        of mails: the first chunk cleared the whole stretch."""
        self.gather({self.email_code})
        chunk_end = self.cut + timedelta(days=14)
        self.gather({self.email_code}, unattended=False, start=self.cut, end=chunk_end)
        job, *_ = self.gather({self.email_code})
        rest = chunk_end + timedelta(days=1)
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(rest, self.bound))
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).pending_from, rest)
        # The second chunk, up to the bound the sentence names, ends it.
        self.gather({self.email_code}, unattended=False, start=rest, end=self.bound)
        job, *_ = self.gather({self.email_code})
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_a_past_period_ending_before_it_leaves_it_whole(self):
        """[Q2] It searched nothing of the stretch, and cleared it."""
        self.gather({self.email_code})
        self.gather(
            {self.email_code},
            unattended=False,
            start=self.cut - timedelta(days=30),
            end=self.cut - timedelta(days=10),
        )
        job, *_ = self.gather({self.email_code})
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(self.cut, self.bound))

    def test_the_dates_achats_offers_after_a_failed_past_period_leave_the_rest_said(self):
        """[Q2] Achats offers a past period another source failed again, with
        its own end, from the stretch's start: posted as offered, the search
        stops at that end and clears nothing past it."""
        self.gather({self.email_code})

        def portal_fails(job, invoice_type, source, code, *args, **kwargs):
            job.update_progress(code, error="Espace client : refusé")
            return 0, 0

        with mock.patch("invoices.tasks._gather_website", side_effect=portal_fails):
            self.gather(
                {self.email_code, self.portal_code}, unattended=False, start=self.days_ago(150), end=self.days_ago(140)
            )
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        start, end = page.context["default_start_date"], page.context["default_end_date"]
        self.assertEqual((start, end), (self.cut, self.days_ago(140)))
        self.gather({self.email_code}, unattended=False, start=start, end=end)
        job, *_ = self.gather({self.email_code})
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(self.days_ago(139), self.bound))

    def achats_start(self) -> date:
        return self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer").context["default_start_date"]


class NeverSearchedTests(CoverageCase):
    """[Q4] A source never searched counts a search only if it started on or
    before its own start - where an automatic run would have started it. A
    manual gather from Achats' default start (yesterday) set it searched up
    to today, and its own start was never searched."""

    def setUp(self):
        super().setUp()
        self.yesterday_s_invoice = {"side_effect": self.brings_in_yesterday_s_invoice}

    def brings_in_yesterday_s_invoice(self, *args, **kwargs):
        # What the search imports moves the supplier's newest invoice.
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(1))
        return []

    def test_a_manual_gather_from_yesterday_leaves_its_own_start_to_the_next_automatic_run(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(40 + OVERLAP_DAYS))

    def test_even_when_that_gather_brought_in_a_newer_invoice(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1), email=self.yesterday_s_invoice)
        self.assertEqual(self.email_start(), self.days_ago(40 + OVERLAP_DAYS))

    def test_with_no_invoice_its_own_start_is_the_lookback(self):
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(DEFAULT_LOOKBACK_DAYS))

    def test_a_pending_stretch_and_a_manual_gather_from_yesterday(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(200))
        bound, cut = self.days_ago(DEFAULT_LOOKBACK_DAYS), self.days_ago(200 + OVERLAP_DAYS)
        # A first automatic run failing: the stretch is pending, nothing searched.
        self.gather({self.email_code}, email={"side_effect": OSError("connexion perdue")})
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], bound)
        self.assertEqual(job.progress[self.email_code]["catch_up"], catch_up_sentence(cut, bound))

    def test_a_manual_gather_from_its_own_start_counts(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        self.gather({self.email_code}, unattended=False, start=self.days_ago(40 + OVERLAP_DAYS))
        self.assertEqual(self.email_start(), self.days_ago(OVERLAP_DAYS))


class SearchSettingsChangedTests(CoverageCase):
    """[Q3] A source whose patterns missed its mails searched cleanly, « 0
    trouvée », and its coverage moved to today: corrected, its search started
    from today - 3 and the days its old patterns missed were never searched.
    A change of its search settings lowers its coverage to its own start."""

    def setUp(self):
        super().setUp()
        # Its newest invoice: before its sending address changed.
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(25))
        # Every run since: clean, nothing found.
        self.gather({self.email_code})
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def save_source(self, **changes):
        values = {
            "name": self.cave.name,
            "supplier": str(self.cave.supplier_id),
            "supplier_was": str(self.cave.supplier_id),
            "source_kind": "EMAIL",
            "parser_key": "",
            "is_active": "on",
            "action": "save",
            "sender_pattern": self.cave.email_source.sender_pattern,
            "subject_pattern": "",
            "body_pattern": "",
            "attachment_pattern": self.cave.email_source.attachment_pattern,
            **changes,
        }
        response = self.client.post(reverse("invoices:invoice_type_update", args=[self.cave.pk]), values)
        self.assertEqual(response.status_code, 302)

    def test_a_corrected_sender_searches_again_from_its_own_start(self):
        self.save_source(sender_pattern=r"compta@cave\.exemple")
        self.assertEqual(self.email_start(), self.days_ago(40 + OVERLAP_DAYS))

    def test_each_search_setting_counts(self):
        for field, value in (
            ("subject_pattern", "Facture"),
            ("body_pattern", "montant"),
            ("attachment_pattern", r"\.pdf$"),
        ):
            with self.subTest(field=field):
                GatherCoverage.objects.filter(code=self.email_code).update(searched_until=self.today)
                self.save_source(**{field: value})
                self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(40))

    def test_a_manual_gather_from_achats_default_in_between_does_not_undo_it(self):
        self.save_source(sender_pattern=r"compta@cave\.exemple")
        self.gather({self.email_code}, unattended=False, start=self.days_ago(1))
        self.assertEqual(self.email_start(), self.days_ago(40 + OVERLAP_DAYS))

    def test_saving_its_name_alone_changes_nothing(self):
        self.save_source(name="Cave Exemple - Toutes les factures")
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def test_never_moved_forward(self):
        GatherCoverage.objects.filter(code=self.email_code).update(searched_until=self.days_ago(60))
        self.save_source(sender_pattern=r"compta@cave\.exemple")
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(60))

    def test_a_source_switched_from_its_portal_to_the_mailbox(self):
        InvoiceType.objects.filter(pk=self.cave.pk).update(source_kind=InvoiceType.SourceKind.WEBSITE)
        self.save_source()
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(40))


class SlipsCoverageTests(CoverageCase):
    """The slip formats the same way."""

    def slips_start(self, *, own=None, **kwargs) -> date:
        with mock.patch("returnables.mail.fetch_start", return_value=own or self.days_ago(DEFAULT_LOOKBACK_DAYS)):
            _job, _email, slips, _emit = self.gather({self.slips_code}, **kwargs)
        return slips.call_args.args[0]

    def test_searched_yesterday_starts_yesterday_less_the_overlap(self):
        finished_gather(self.slips_code, 1)
        self.assertEqual(self.slips_start(), self.days_ago(1 + OVERLAP_DAYS))

    def test_a_newer_bon_never_jumps_the_hole(self):
        finished_gather(self.slips_code, 25)
        self.assertEqual(self.slips_start(own=self.days_ago(1)), self.days_ago(25 + OVERLAP_DAYS))

    def test_a_manual_gather_from_a_later_start_leaves_the_hole(self):
        finished_gather(self.slips_code, 20)
        self.slips_start(own=self.days_ago(1), unattended=False, start=self.days_ago(1))
        self.assertEqual(self.slips_start(own=self.days_ago(1)), self.days_ago(20 + OVERLAP_DAYS))

    def test_a_killed_slips_run_leaves_the_format_at_its_own_start(self):
        with self.assertRaises(Killed):
            self.slips_start(slips={"side_effect": Killed()})
        ScrapeJob.objects.update(status=ScrapeJob.Status.FAILED)
        self.assertEqual(self.slips_start(own=self.days_ago(50)), self.days_ago(50))

    def caught_up_from_achats(self) -> None:
        """« Récupérer » with the dates Achats offers: a slips search never
        reaches further back than the mailbox's floor (returnables.mail)."""
        from returnables.mail import MAX_LOOKBACK_DAYS

        offered = PendingCatchUpTests.achats_start(self)
        self.assertLess(offered, self.days_ago(MAX_LOOKBACK_DAYS))
        _job, _email, slips, _emit = self.gather({self.slips_code}, unattended=False, start=offered)
        self.assertEqual(slips.call_args.args[0], self.days_ago(MAX_LOOKBACK_DAYS))

    def test_a_stretch_older_than_the_floor_is_caught_up_from_the_floor(self):
        """[Q6] Left inactive over a year: the stretch began before any slips
        search can start, and nothing could ever clear it - the catch-up
        sentence and the failed alert came back every day."""
        from returnables.mail import MAX_LOOKBACK_DAYS

        GatherCoverage.objects.create(code=self.slips_code, searched_until=self.days_ago(MAX_LOOKBACK_DAYS + 30))
        job, *_ = self.gather({self.slips_code})
        self.assertIn("catch_up", job.progress[self.slips_code])
        self.caught_up_from_achats()
        job, _email, _slips, emit = self.gather({self.slips_code})
        self.assertNotIn("catch_up", job.progress[self.slips_code])
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "nothing"))

    def test_a_stretch_left_open_until_it_aged_past_the_floor(self):
        from returnables.mail import MAX_LOOKBACK_DAYS

        GatherCoverage.objects.create(
            code=self.slips_code, searched_until=self.today, pending_from=self.days_ago(MAX_LOOKBACK_DAYS + 10)
        )
        self.caught_up_from_achats()
        job, *_ = self.gather({self.slips_code})
        self.assertNotIn("catch_up", job.progress[self.slips_code])


class RestartDuringAGatherTests(CoverageCase):
    """[S0] A source's search settings saved (coverage.restart) while a
    gather that loaded the OLD ones is still running: that gather searched
    with the old patterns, and recording what it covered undid the restart -
    the days the old patterns missed were never searched again. It records
    nothing for that source; the next run searches from the restart."""

    def slip_format(self):
        from returnables.models import SlipFormat

        return SlipFormat.objects.get(pk=int(self.slips_code.split("-")[1]))

    def save_format_with_another_sender(self) -> date:
        """What returnables/views._format_page does on a pattern change."""
        from invoices import coverage
        from returnables.mail import fetch_start

        with transaction.atomic():
            fmt = self.slip_format()
            fmt.sender_pattern = r"bons@autre\.exemple"
            fmt.save(update_fields=["sender_pattern"])
            coverage.restart(self.slips_code, fetch_start(fmt, None, self.today))
        return GatherCoverage.objects.get(code=self.slips_code).searched_until

    def save_email_source(self, invoice_type, **changes):
        """The source's page, saved as a person saves it."""
        source = invoice_type.email_source
        values = {
            "name": invoice_type.name,
            "supplier": str(invoice_type.supplier_id),
            "supplier_was": str(invoice_type.supplier_id),
            "source_kind": "EMAIL",
            "parser_key": "",
            "is_active": "on",
            "action": "save",
            "sender_pattern": source.sender_pattern,
            "subject_pattern": source.subject_pattern,
            "body_pattern": source.body_pattern,
            "attachment_pattern": source.attachment_pattern,
            **changes,
        }
        response = self.client.post(reverse("invoices:invoice_type_update", args=[invoice_type.pk]), values)
        self.assertEqual(response.status_code, 302)

    def test_a_format_saved_while_an_automatic_run_searched_the_mailbox(self):
        GatherCoverage.objects.create(code=self.slips_code, searched_until=self.today)
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.today)
        restarted = []

        def mailbox(*args, **kwargs):
            restarted.append(self.save_format_with_another_sender())
            return []

        job, _email, slips, _emit = self.gather({self.email_code, self.slips_code}, email={"side_effect": mailbox})
        # Searched with the patterns it had loaded...
        self.assertEqual(slips.call_args.kwargs["sender_pattern"], r"mphone@uba\.paris")
        # ...so it covered nothing the new ones look for.
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, restarted[0])
        self.assertIn("couverture non enregistrée", job.log)
        # The mailbox type, unchanged, recorded its search as ever.
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)

    def test_a_format_saved_while_a_manual_gather_searched_the_mailbox(self):
        GatherCoverage.objects.create(code=self.slips_code, searched_until=self.today)
        restarted = []

        def mailbox(*args, **kwargs):
            restarted.append(self.save_format_with_another_sender())
            return []

        self.gather(
            {self.email_code, self.slips_code},
            unattended=False,
            start=self.days_ago(1),
            email={"side_effect": mailbox},
        )
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, restarted[0])

    def test_a_format_saved_during_its_own_search(self):
        GatherCoverage.objects.create(code=self.slips_code, searched_until=self.today)
        restarted = []

        def own_search(*args, **kwargs):
            restarted.append(self.save_format_with_another_sender())
            return []

        self.gather({self.slips_code}, unattended=False, start=self.days_ago(1), slips={"side_effect": own_search})
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, restarted[0])

    def test_a_second_mailbox_type_saved_while_the_first_was_searched(self):
        second = email_type(make_supplier(code="ZETA_X", name="Zeta Exemple"), name="Zeta Exemple - Factures")
        second_code = f"type-{second.pk}"
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.today)
        GatherCoverage.objects.create(code=second_code, searched_until=self.today)
        senders = []

        def mailbox(*args, **kwargs):
            senders.append(kwargs["sender_pattern"])
            if len(senders) == 1:
                self.save_email_source(second, sender_pattern=r"compta@zeta\.exemple")
            return []

        self.gather({self.email_code, second_code}, email={"side_effect": mailbox})
        # Both searched with what was loaded before the save.
        self.assertEqual(senders, ["factures@cave.exemple", "factures@cave.exemple"])
        restarted = self.today - timedelta(days=DEFAULT_LOOKBACK_DAYS) + timedelta(days=OVERLAP_DAYS)
        self.assertEqual(GatherCoverage.objects.get(code=second_code).searched_until, restarted)
        # The next run searches it from there, with its new sender.
        senders.clear()
        self.gather({second_code}, email={"side_effect": mailbox})
        self.assertEqual(senders, [r"compta@zeta\.exemple"])
        self.assertEqual(GatherCoverage.objects.get(code=second_code).searched_until, self.today)

    def test_a_mailbox_type_saved_during_its_own_search(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(2))
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.today)

        def own_search(*args, **kwargs):
            self.save_email_source(self.cave, sender_pattern=r"compta@cave\.exemple")
            return []

        self.gather({self.email_code}, email={"side_effect": own_search})
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(2))

    def test_its_name_saved_meanwhile_lets_the_search_count(self):
        GatherCoverage.objects.create(code=self.email_code, searched_until=self.days_ago(10))

        def own_search(*args, **kwargs):
            self.save_email_source(self.cave, name="Cave Exemple - Toutes les factures")
            return []

        self.gather({self.email_code}, email={"side_effect": own_search})
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.today)


class NeverSearchedPastPeriodTests(CoverageCase):
    """[S1] Never searched, a search counts only if it started on or before
    its own start AND reached it: a past period ending before it covered
    nothing an automatic run would search, and set the source's coverage
    months back - a stretch to catch up, a failed alert every day."""

    def test_a_past_period_ending_before_its_own_start_leaves_it_there(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(10))
        self.gather({self.email_code}, unattended=False, start=self.days_ago(200), end=self.days_ago(170))
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(10 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])
        self.assertIsNone(GatherCoverage.objects.get(code=self.email_code).pending_from)

    def test_a_past_period_reaching_its_own_start_counts(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(10))
        self.gather({self.email_code}, unattended=False, start=self.days_ago(200), end=self.days_ago(13))
        self.assertEqual(GatherCoverage.objects.get(code=self.email_code).searched_until, self.days_ago(13))

    def slips_own_start(self) -> date:
        """The format's own start, whatever dates a person posts."""
        from returnables.mail import fetch_start
        from returnables.models import SlipFormat

        return fetch_start(SlipFormat.objects.get(pk=int(self.slips_code.split("-")[1])), None, self.today)

    def test_a_slip_format_s_past_period_ending_before_its_own_start_leaves_it_there(self):
        # A slips search starts at the posted date when it is earlier than
        # the format's own start: that start is no measure of where an
        # automatic run would have searched from.
        self.gather({self.slips_code}, unattended=False, start=self.days_ago(200), end=self.days_ago(170))
        job, _email, slips, _emit = self.gather({self.slips_code})
        self.assertEqual(slips.call_args.args[0], self.slips_own_start())
        self.assertNotIn("catch_up", job.progress[self.slips_code])
        self.assertIsNone(GatherCoverage.objects.get(code=self.slips_code).pending_from)

    def test_a_slip_format_s_past_period_reaching_its_own_start_counts(self):
        own = self.slips_own_start()
        self.gather({self.slips_code}, unattended=False, start=self.days_ago(200), end=own)
        self.assertEqual(GatherCoverage.objects.get(code=self.slips_code).searched_until, own)


class HistoryFurthestReachTests(CoverageCase):
    """[S2] Read from the history, a source is covered as far as the
    furthest clean finished gather reached - not the newest one's: a past
    period gathered again last hid an older gather that searched up to its
    own day, and the coverage jumped months back."""

    def test_a_past_period_regathered_last_hides_no_older_gather(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        finished_gather(self.email_code, 5, range_end=self.days_ago(5))
        finished_gather(self.email_code, 1, trigger=ScrapeJob.Trigger.MANUAL, range_end=self.days_ago(220))
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(5 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_the_furthest_of_several_past_periods(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        finished_gather(self.email_code, 9, range_end=self.days_ago(30))
        finished_gather(self.email_code, 6, range_end=self.days_ago(20))
        finished_gather(self.email_code, 3, error=True, range_end=self.days_ago(3))
        finished_gather(self.email_code, 2, range_end=self.days_ago(60))
        self.assertEqual(self.email_start(), self.days_ago(20 + OVERLAP_DAYS))


class StretchEndTests(CoverageCase):
    """[S3] The stretch ends the day before the bound that CUT it, kept
    (GatherCoverage.pending_until): a chunk caught up to the bound its
    sentence named a few days ago ends it - the automatic runs searched every
    day from that bound on - and the sentence's « au » no longer drifts with
    today's bound."""

    code = "type-exemple"

    def cut_then_follow(self, own):
        """Cut five days ago, then one automatic run a day up to today."""
        from invoices import coverage

        cut_day = self.days_ago(5)
        where = coverage.unattended_start(self.code, own, today=cut_day)
        self.assertTrue(where.bounded)
        cut_bound = coverage.lookback_bound(cut_day)
        self.assertEqual(where.catch_up, catch_up_sentence(own, cut_bound))
        coverage.searched(self.code, where.start, cut_day, own=own, bounded=True, today=cut_day)
        for back in range(4, -1, -1):
            day = self.days_ago(back)
            where = coverage.unattended_start(self.code, own, today=day)
            self.assertFalse(where.bounded)
            self.assertEqual(where.catch_up, catch_up_sentence(own, cut_bound))
            coverage.searched(self.code, where.start, day, own=own, today=day)
        return cut_bound

    def test_a_chunk_up_to_the_bound_that_cut_it_ends_it(self):
        from invoices import coverage

        own = self.days_ago(150)
        cut_bound = self.cut_then_follow(own)
        self.assertEqual(GatherCoverage.objects.get(code=self.code).pending_until, cut_bound - timedelta(days=1))
        coverage.searched(self.code, own, cut_bound, own=own)
        row = GatherCoverage.objects.get(code=self.code)
        self.assertEqual((row.pending_from, row.pending_until), (None, None))
        self.assertEqual(coverage.unattended_start(self.code, own).catch_up, "")

    def test_a_shorter_chunk_moves_its_start_and_keeps_its_end(self):
        from invoices import coverage

        own = self.days_ago(150)
        cut_bound = self.cut_then_follow(own)
        coverage.searched(self.code, own, self.days_ago(120), own=own)
        row = GatherCoverage.objects.get(code=self.code)
        self.assertEqual((row.pending_from, row.pending_until), (self.days_ago(119), cut_bound - timedelta(days=1)))
        self.assertEqual(
            coverage.unattended_start(self.code, own).catch_up, catch_up_sentence(self.days_ago(119), cut_bound)
        )

    def test_cut_again_after_a_restart_the_stretch_ends_at_the_latest_cut(self):
        from invoices import coverage

        own = self.days_ago(150)
        self.cut_then_follow(own)
        # Its patterns changed: from its own start again, cut at today's bound.
        coverage.restart(self.code, self.days_ago(140))
        where = coverage.unattended_start(self.code, own)
        bound = coverage.lookback_bound(self.today)
        self.assertTrue(where.bounded)
        row = GatherCoverage.objects.get(code=self.code)
        self.assertEqual((row.pending_from, row.pending_until), (own, bound - timedelta(days=1)))
        self.assertEqual(where.catch_up, catch_up_sentence(own, bound))

    def test_through_the_task_the_chunk_the_older_sentence_named_ends_it(self):
        cut_bound = self.days_ago(5 + DEFAULT_LOOKBACK_DAYS)
        GatherCoverage.objects.create(
            code=self.email_code,
            searched_until=self.today,
            pending_from=self.days_ago(150),
            pending_until=cut_bound - timedelta(days=1),
        )
        self.gather({self.email_code}, unattended=False, start=self.days_ago(150), end=cut_bound)
        job, _email, _slips, emit = self.gather({self.email_code})
        self.assertNotIn("catch_up", job.progress[self.email_code])
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "nothing"))

    def test_a_row_written_before_its_end_was_kept_ends_at_today_s_bound(self):
        from invoices import coverage

        GatherCoverage.objects.create(code=self.code, searched_until=self.today, pending_from=self.days_ago(150))
        bound = coverage.lookback_bound(self.today)
        self.assertEqual(
            coverage.unattended_start(self.code, self.days_ago(150)).catch_up,
            catch_up_sentence(self.days_ago(150), bound),
        )
        coverage.searched(self.code, self.days_ago(150), bound - timedelta(days=1), own=self.days_ago(150))
        self.assertIsNone(GatherCoverage.objects.get(code=self.code).pending_from)


class PendingOnTheCardTests(PageCase):
    def test_the_rule_card_lists_its_sources_pending_catch_ups(self):
        from invoices.models import GatherCoverage

        make_rule(name="Factures", sources=[self.email_code], every_minutes=60)
        pending = timezone.localdate() - timedelta(days=150)
        GatherCoverage.objects.create(code=self.email_code, searched_until=timezone.localdate(), pending_from=pending)
        text = self.text(self.html(PAGE))
        bound = timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        self.assertIn(f"Cave Exemple - Factures : {catch_up_sentence(pending, bound)}", text)

    def test_the_card_names_the_bound_that_cut_it(self):
        """[S3] Not today's bound, which moves a day a day."""
        from invoices.models import GatherCoverage

        make_rule(name="Factures", sources=[self.email_code], every_minutes=60)
        today = timezone.localdate()
        pending, end = today - timedelta(days=150), today - timedelta(days=96)
        GatherCoverage.objects.create(
            code=self.email_code, searched_until=today, pending_from=pending, pending_until=end
        )
        text = self.text(self.html(PAGE))
        self.assertIn(f"Cave Exemple - Factures : {catch_up_sentence(pending, end + timedelta(days=1))}", text)

    def test_a_source_of_another_rule_is_not_listed(self):
        from invoices.models import GatherCoverage

        make_rule(name="Bons")
        GatherCoverage.objects.create(code=self.email_code, pending_from=timezone.localdate() - timedelta(days=150))
        self.assertNotIn("Rattrapage", self.text(self.html(PAGE)))
