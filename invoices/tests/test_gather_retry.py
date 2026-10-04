"""A gather that ended - failed, cancelled or done - can be run again from
the page, for the period it was asked for.

It could not: the button was drawn disabled while the gather ran, and when
it ended only the status card below it was redrawn, so the button stayed
disabled until the page was reloaded by hand - and the reload put back the
default start date, not the 01/01/2026 typed. A gather whose thread died
(the dev server reloading) stayed "running" for good: only a new gather
reaped it, and the disabled button forbade one.
(test_gather_retry_browser.py clicks it in a real browser.)

Data invented.
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import InvoiceType, ScrapeJob
from recipes.models import SalesImportJob


def gather_job(status, **fields):
    return ScrapeJob.objects.create(kind=ScrapeJob.Kind.GATHER, status=status, **fields)


class GatherButtonTests(TestCase):
    def page(self):
        return self.client.get(reverse("invoices:invoice_list"))

    def test_the_button_and_the_live_dot_follow_the_status_card(self):
        """Drawn disabled while the card polls; the page script lets them go
        as soon as the card says the gather is over (ui.js, data-job-control)."""
        gather_job(ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        page = self.page()
        self.assertContains(page, 'data-job-control="gather-status" disabled')
        self.assertContains(page, "data-job-active")

    def test_a_finished_gather_says_it_is_over(self):
        job = gather_job(ScrapeJob.Status.FAILED)
        card = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertNotContains(card, "data-job-active")
        self.assertNotContains(card, "hx-get")

    def test_a_gather_whose_thread_died_is_reaped_when_polled(self):
        """Nothing else would ever reap it: the page kept polling a run
        that no thread was running, and the button stayed disabled."""
        job = gather_job(ScrapeJob.Status.RUNNING)
        ScrapeJob.objects.filter(pk=job.pk).update(last_heartbeat=timezone.now() - timedelta(minutes=30))
        card = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        self.assertNotContains(card, "hx-get")

    def test_the_page_reaps_it_too(self):
        job = gather_job(ScrapeJob.Status.RUNNING)
        ScrapeJob.objects.filter(pk=job.pk).update(last_heartbeat=timezone.now() - timedelta(minutes=30))
        page = self.page()
        self.assertNotContains(page, 'data-job-control="gather-status" disabled')


class GatherOutcomeTests(TestCase):
    """What the card says once a gather is over - in plain view, not only in
    the folded log."""

    def card(self, job):
        return self.client.get(reverse("invoices:gather_status", args=[job.pk]))

    def test_a_source_in_error_is_said_beside_the_status(self):
        job = gather_job(
            ScrapeJob.Status.SUCCESS,
            progress={
                "METRO": {
                    "label": "Metro",
                    "found": 0,
                    "imported": 0,
                    "error": "Metro a bloqué la connexion (pare-feu).",
                },
                "type-1": {"label": "Grossiste", "found": 2, "imported": 2},
            },
        )
        card = self.card(job)
        self.assertContains(card, "1 source en échec")
        self.assertContains(card, '<span class="field-error">Metro a bloqué la connexion (pare-feu).</span>', html=True)

    def test_a_failed_gather_says_why(self):
        """The pill said "Échoué", Metro 0/0; the reason sat in the log."""
        job = gather_job(ScrapeJob.Status.FAILED)
        job.append_log("Gather run failed: la base de données est verrouillée")
        self.assertContains(self.card(job), "la base de données est verrouillée")

    def test_a_test_job_is_unchanged(self):
        job = ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST, status=ScrapeJob.Status.SUCCESS, progress={})
        self.assertNotContains(self.card(job), "en échec")


class GatherPeriodTests(TestCase):
    def start_date_shown(self):
        page = self.client.get(reverse("invoices:invoice_list"))
        return page.context["default_start_date"], page.context["default_end_date"]

    def test_a_failed_gather_offers_its_period_again(self):
        gather_job(ScrapeJob.Status.FAILED, range_start=date(2026, 1, 1), range_end=date(2026, 9, 18))
        start, end = self.start_date_shown()
        self.assertEqual(start, date(2026, 1, 1))
        self.assertGreaterEqual(end, date(2026, 9, 18))

    def test_a_gather_with_a_source_in_error_offers_its_period_again(self):
        gather_job(
            ScrapeJob.Status.SUCCESS,
            range_start=date(2026, 1, 1),
            range_end=date(2026, 9, 18),
            progress={"METRO": {"label": "Metro", "error": "Metro a bloqué la connexion."}},
        )
        self.assertEqual(self.start_date_shown()[0], date(2026, 1, 1))

    def test_a_source_failing_the_same_way_again_stops_holding_the_period(self):
        """A portal asking for a code every time kept every gather on
        01/01, rescanning the mailbox from there for good. Its line says it
        is in error; the others go on from what arrived since."""
        failing = {"type-5": {"label": "Portail", "found": 0, "imported": 0, "error": "Code SMS demandé."}}
        gather_job(
            ScrapeJob.Status.SUCCESS, range_start=date(2026, 1, 1), range_end=date(2026, 9, 18), progress=failing
        )
        gather_job(
            ScrapeJob.Status.SUCCESS, range_start=date(2026, 1, 1), range_end=date(2026, 9, 19), progress=failing
        )
        self.assertNotEqual(self.start_date_shown()[0], date(2026, 1, 1))

    def test_a_gather_that_went_well_offers_what_arrived_since(self):
        gather_job(ScrapeJob.Status.SUCCESS, range_start=date(2026, 1, 1), range_end=date(2026, 9, 18), progress={})
        self.assertNotEqual(self.start_date_shown()[0], date(2026, 1, 1))


class GatherRequestTests(TestCase):
    def post(self, sources):
        with mock.patch("invoices.views.threading.Thread"):
            return self.client.post(
                reverse("invoices:gather"),
                {"start_date": "2026-01-01", "end_date": "2026-09-18", "sources": sources},
                follow=True,
            )

    def test_nothing_ticked_starts_nothing_and_says_so(self):
        """It ran, found nothing, and said "Terminé"."""
        response = self.post([])
        self.assertFalse(ScrapeJob.objects.exists())
        self.assertContains(response, "Aucune source cochée")

    def test_a_second_gather_while_one_runs_is_said(self):
        gather_job(ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        response = self.post(["METRO"])
        self.assertEqual(ScrapeJob.objects.count(), 1)
        self.assertContains(response, "déjà en cours")


class GatherPeriodBoundsTests(TestCase):
    """A year typed as « 26 » is 0026 in a date box: Metro was searched in
    8 028 windows of 91 days from 0026-09-01, in one signed-in session, and
    every mailbox from its first mail - for hours, blocking every other
    gather and the deploy. A period starts in 2000 at the earliest (the
    rule for a document's date, forms.EARLIEST_DOCUMENT_DATE), ends today
    at the latest, and starts before it ends."""

    def post(self, start, end):
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(
                reverse("invoices:gather"), {"start_date": start, "end_date": end, "sources": ["METRO"]}, follow=True
            )
        return response, thread

    def test_a_year_typed_as_two_digits_starts_nothing(self):
        response, thread = self.post("0026-09-01", timezone.localdate().isoformat())
        self.assertFalse(ScrapeJob.objects.exists())
        thread.assert_not_called()
        self.assertContains(response, "Période impossible")

    def test_an_end_in_the_future_starts_nothing(self):
        response, _thread = self.post("2026-09-01", "9999-12-31")
        self.assertFalse(ScrapeJob.objects.exists())
        self.assertContains(response, "Période impossible")

    def test_a_start_after_the_end_starts_nothing(self):
        response, _thread = self.post("2026-09-18", "2026-09-01")
        self.assertFalse(ScrapeJob.objects.exists())
        self.assertContains(response, "Période impossible")

    def test_a_period_left_blank_still_starts(self):
        self.post("", "")
        self.assertEqual(ScrapeJob.objects.count(), 1)

    def test_a_period_from_2000_to_today_starts(self):
        """Alone: a second post would meet the first's job and be turned away
        as « déjà en cours » before its period was looked at."""
        today = timezone.localdate()
        self.post("2000-01-01", today.isoformat())
        job = ScrapeJob.objects.get()
        self.assertEqual((job.range_start, job.range_end), (date(2000, 1, 1), today))

    def test_the_boxes_say_so_to_the_browser(self):
        page = self.client.get(reverse("invoices:invoice_list"))
        today = timezone.localdate().isoformat()
        self.assertContains(page, f'name="start_date" min="2000-01-01" max="{today}"')
        self.assertContains(page, f'name="end_date" min="2000-01-01" max="{today}"')

    def test_a_source_test_takes_the_same_rule(self):
        from invoices.forms import gather_range_problem

        self.assertTrue(gather_range_problem(date(26, 9, 1), None))
        self.assertTrue(gather_range_problem(None, timezone.localdate() + timedelta(days=1)))
        self.assertTrue(gather_range_problem(date(2026, 9, 2), date(2026, 9, 1)))
        self.assertEqual(gather_range_problem(None, None), "")
        self.assertEqual(gather_range_problem(date(2000, 1, 1), timezone.localdate()), "")

    def mailbox(self, action):
        return {
            "name": "Traiteur Exemple - Factures",
            "supplier": "new",
            "new_name": "Traiteur Exemple",
            "source_kind": "EMAIL",
            "parser_key": "",
            "is_active": "on",
            "action": action,
            "sender_pattern": r"factures@traiteur\.exemple",
            "subject_pattern": "",
            "body_pattern": "",
            "attachment_pattern": r"\.pdf$",
            "test_start_date": "0026-09-01",
            "test_end_date": "2026-09-01",
        }

    def test_a_mailbox_test_says_why_it_starts_nothing(self):
        """Put on the form, the refusal sat on a box the page draws by hand,
        without its errors: « Tester » did nothing, and said nothing."""
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(reverse("invoices:invoice_type_create"), self.mailbox("test"))
        thread.assert_not_called()
        self.assertFalse(ScrapeJob.objects.exists())
        self.assertContains(response, "Période impossible")

    def test_a_mailbox_saves_whatever_its_test_dates(self):
        """The test dates are only « Tester »'s: they never stopped a save."""
        response = self.client.post(reverse("invoices:invoice_type_create"), self.mailbox("save"))
        self.assertRedirects(response, reverse("invoices:invoice_type_list"), fetch_redirect_response=False)
        self.assertTrue(InvoiceType.objects.filter(name="Traiteur Exemple - Factures").exists())


class MetroPauseOnThePageTests(TestCase):
    """Metro left alone after its firewall refused: said on the gather card,
    its box out of reach - the browser re-ticked a box it remembered - and
    one sign-in only on a separate, unremembered request."""

    def page(self):
        return self.client.get(reverse("invoices:invoice_list"))

    def test_a_paused_metro_is_said_and_cannot_be_ticked(self):
        from invoices.scrapers.metro import record_block

        record_block("#18.0000000.1700000000.00000abc")
        page = self.page()
        self.assertContains(page, 'value="METRO" disabled')
        self.assertContains(page, "#18.0000000.1700000000.00000abc")
        self.assertContains(page, 'name="metro_now"')
        self.assertNotContains(page, 'name="metro_now" checked')

    def test_metro_not_paused_is_offered_as_before(self):
        page = self.page()
        self.assertContains(page, 'value="METRO" checked')
        self.assertNotContains(page, 'name="metro_now"')

    def test_one_sign_in_asked_for_names_metro_and_passes_the_pause(self):
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(
                reverse("invoices:gather"),
                {
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-18",
                    "sources": ["type-1"],
                    "metro_now": "on",
                },
            )
        args = thread.call_args.kwargs["args"]
        self.assertIn("METRO", args[3])
        self.assertTrue(args[4])

    def test_a_note_is_not_a_failure(self):
        job = gather_job(
            ScrapeJob.Status.SUCCESS,
            progress={
                "METRO": {"label": "Metro", "found": 0, "imported": 0, "note": "Metro a déjà été consulté le 18/09."},
            },
        )
        card = self.client.get(reverse("invoices:gather_status", args=[job.pk]))
        self.assertContains(card, "Metro a déjà été consulté le 18/09.")
        self.assertNotContains(card, "en échec")


class SalesImportButtonTests(TestCase):
    """The till import's button had the same fault."""

    def test_the_button_follows_its_status_card(self):
        SalesImportJob.objects.create(
            status=SalesImportJob.Status.RUNNING,
            range_start=date(2026, 1, 1),
            range_end=date(2026, 1, 31),
            last_heartbeat=timezone.now(),
        )
        page = self.client.get(reverse("recipes:sales_list"))
        self.assertContains(page, 'data-job-control="sales-import-status" disabled')
        self.assertContains(page, "data-job-active")

    def test_a_dead_import_is_reaped_when_polled(self):
        job = SalesImportJob.objects.create(
            status=SalesImportJob.Status.RUNNING, range_start=date(2026, 1, 1), range_end=date(2026, 1, 31)
        )
        SalesImportJob.objects.filter(pk=job.pk).update(last_heartbeat=timezone.now() - timedelta(minutes=30))
        self.client.get(reverse("recipes:sales_import_status", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
