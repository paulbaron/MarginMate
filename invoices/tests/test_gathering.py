"""The one start of a gather (invoices/gathering.py), shared by « Récupérer »
and the automatic gathers: the stale runs reaped, the active check and the
job created in one transaction, the thread started once it committed - and
`trigger_gather` saying exactly what it said before.

No real gather starts: `invoices.gathering.threading.Thread` is patched in
every test. Data invented.
"""

from datetime import date, timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices import gathering
from invoices.models import ScrapeJob
from invoices.tasks import gather_invoices_task


def running_job(**fields):
    return ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now(), **fields)


class StartGatherTests(TestCase):
    def start(self, codes=("type-7",), start=date(2026, 9, 1), end=date(2026, 9, 18), **kwargs):
        kwargs.setdefault("metro_now", False)
        kwargs.setdefault("trigger", ScrapeJob.Trigger.MANUAL)
        with mock.patch("invoices.gathering.threading.Thread") as thread:
            job = gathering.start_gather(set(codes), start, end, **kwargs)
        return job, thread

    def test_a_manual_gather_starts_its_bound_thread_with_the_old_arguments(self):
        job, thread = self.start(codes={"type-7", "bons-2"}, metro_now=True)
        self.assertEqual(job.trigger, ScrapeJob.Trigger.MANUAL)
        self.assertIsNone(job.auto_gather_id)
        self.assertEqual((job.range_start, job.range_end), (date(2026, 9, 1), date(2026, 9, 18)))
        call = thread.call_args.kwargs
        self.assertEqual(call["args"], (job.id, date(2026, 9, 1), date(2026, 9, 18), {"type-7", "bons-2"}, True))
        self.assertNotIn("kwargs", call)
        self.assertTrue(call["daemon"])
        self.assertIs(call["target"].__wrapped__, gather_invoices_task)
        thread.return_value.start.assert_called_once_with()

    def test_an_automatic_gather_says_whose_slot_it_is_and_runs_unattended(self):
        job, thread = self.start(
            codes={"bons-2"},
            start=None,
            end=None,
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            auto_gather_id=4,
            unattended=True,
        )
        job.refresh_from_db()
        self.assertEqual((job.trigger, job.auto_gather_id), (ScrapeJob.Trigger.AUTOMATIC, 4))
        self.assertTrue(job.is_automatic)
        self.assertEqual(thread.call_args.kwargs["args"], (job.id, None, None, {"bons-2"}, False))
        self.assertEqual(thread.call_args.kwargs["kwargs"], {"unattended": True})

    def test_nothing_starts_beside_a_running_gather(self):
        running = running_job()
        job, thread = self.start()
        self.assertIsNone(job)
        thread.assert_not_called()
        self.assertEqual(list(ScrapeJob.objects.values_list("pk", flat=True)), [running.pk])

    def test_a_pending_gather_counts_as_running_and_a_pattern_test_does_not(self):
        ScrapeJob.objects.create(status=ScrapeJob.Status.PENDING, last_heartbeat=timezone.now())
        self.assertIsNone(self.start()[0])
        ScrapeJob.objects.all().delete()
        ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST, status=ScrapeJob.Status.RUNNING)
        self.assertIsNotNone(self.start()[0])

    def test_a_run_dead_without_a_word_is_reaped_first(self):
        dead = running_job()
        ScrapeJob.objects.filter(pk=dead.pk).update(
            last_heartbeat=timezone.now() - ScrapeJob.STALE_AFTER - timedelta(minutes=1)
        )
        job, thread = self.start()
        self.assertIsNotNone(job)
        dead.refresh_from_db()
        self.assertEqual(dead.status, ScrapeJob.Status.FAILED)
        thread.return_value.start.assert_called_once()

    def test_the_check_and_the_creation_share_one_transaction(self):
        """IMMEDIATE (config/settings.py): the write lock is taken as the
        block opens, so two starters cannot both see nothing running."""
        depth = {}
        outside = len(connection.atomic_blocks)
        real_active, real_create = gathering.active_gather, ScrapeJob.objects.create

        def active(*args, **kwargs):
            depth["check"] = len(connection.atomic_blocks)
            return real_active(*args, **kwargs)

        def create(**fields):
            depth["create"] = len(connection.atomic_blocks)
            return real_create(**fields)

        with (
            mock.patch("invoices.gathering.active_gather", side_effect=active),
            mock.patch.object(ScrapeJob.objects, "create", side_effect=create),
        ):
            self.start()
        self.assertEqual(depth, {"check": outside + 1, "create": outside + 1})

    def test_no_thread_when_the_job_could_not_be_written(self):
        with (
            mock.patch.object(ScrapeJob.objects, "create", side_effect=RuntimeError("disque plein")),
            mock.patch("invoices.gathering.threading.Thread") as thread,
            self.assertRaises(RuntimeError),
        ):
            gathering.start_gather({"type-7"}, None, None, metro_now=False, trigger=ScrapeJob.Trigger.MANUAL)
        thread.assert_not_called()


class TriggerGatherKeepsItsWordsTests(TestCase):
    """trigger_gather goes through start_gather and says what it said."""

    def post(self, **data):
        with mock.patch("invoices.gathering.threading.Thread") as thread:
            response = self.client.post(reverse("invoices:gather"), data, follow=True)
        return response, thread

    def test_a_gather_asked_for_is_a_manual_one(self):
        _response, thread = self.post(sources=["type-7"], start_date="2026-09-01", end_date="2026-09-18")
        job = ScrapeJob.objects.get()
        self.assertEqual(job.trigger, ScrapeJob.Trigger.MANUAL)
        self.assertEqual(
            thread.call_args.kwargs["args"], (job.id, date(2026, 9, 1), date(2026, 9, 18), {"type-7"}, False)
        )

    def test_a_running_automatic_gather_is_said_running(self):
        running_job(trigger=ScrapeJob.Trigger.AUTOMATIC, auto_gather_id=1)
        response, thread = self.post(sources=["type-7"])
        thread.assert_not_called()
        self.assertEqual(ScrapeJob.objects.count(), 1)
        self.assertContains(response, "Une récupération est déjà en cours : elle s&#x27;affiche ci-dessous.")

    def test_running_beats_nothing_ticked_as_before(self):
        running_job()
        response, _thread = self.post()
        self.assertContains(response, "déjà en cours")
        self.assertNotContains(response, "Aucune source cochée")

    def test_nothing_ticked_and_nothing_running_is_said(self):
        response, thread = self.post()
        thread.assert_not_called()
        self.assertFalse(ScrapeJob.objects.exists())
        self.assertContains(response, "Aucune source cochée : rien à récupérer. Cochez-en au moins une.")

    def test_one_sign_in_to_metro_still_names_it(self):
        _response, thread = self.post(sources=["type-7"], metro_now="on")
        self.assertEqual(thread.call_args.kwargs["args"][3], {"type-7", "METRO"})
        self.assertTrue(thread.call_args.kwargs["args"][4])
