"""What happens when a sales import doesn't finish cleanly.

A background thread cannot be relied on to reach its own `finally`. The dev
server's autoreloader kills it outright on any code change, and the job is
then left RUNNING for ever - which blocks every future run behind "une
récupération est déjà en cours", with a Cancel button that does nothing
because there is no thread left to notice it. That combination is a deadlock
you cannot get out of from the UI, and it is exactly what happened while
fetching three years of sales.
"""

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from recipes.models import SalesImportJob


def make_job(**kwargs):
    kwargs.setdefault("status", SalesImportJob.Status.RUNNING)
    return SalesImportJob.objects.create(**kwargs)


def age(job, minutes):
    """Backdate a job's heartbeat, since started_at is auto_now_add."""
    when = timezone.now() - timedelta(minutes=minutes)
    SalesImportJob.objects.filter(pk=job.pk).update(started_at=when, last_heartbeat=when)
    job.refresh_from_db()
    return job


class StaleJobTests(TestCase):
    def test_a_job_that_has_just_spoken_is_not_stale(self):
        job = make_job()
        job.beat()
        self.assertFalse(job.is_stale)

    def test_a_silent_job_eventually_counts_as_dead(self):
        self.assertTrue(age(make_job(), minutes=30).is_stale)

    def test_a_working_job_is_given_plenty_of_rope(self):
        """One export of three years of tickets takes about a minute to
        generate and fetch; calling that dead would be worse than waiting."""
        self.assertFalse(age(make_job(), minutes=3).is_stale)

    def test_a_finished_job_is_never_stale(self):
        job = age(make_job(status=SalesImportJob.Status.SUCCESS), minutes=120)
        self.assertFalse(job.is_stale)

    def test_a_job_with_no_heartbeat_at_all_falls_back_to_its_start(self):
        job = make_job()
        SalesImportJob.objects.filter(pk=job.pk).update(
            started_at=timezone.now() - timedelta(minutes=30), last_heartbeat=None
        )
        job.refresh_from_db()
        self.assertIsNone(job.last_heartbeat)
        self.assertTrue(job.is_stale)

    def test_reaping_marks_it_failed_and_says_why(self):
        job = age(make_job(), minutes=30)
        self.assertEqual(SalesImportJob.reap_stale(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIsNotNone(job.finished_at)
        self.assertIn("redémarré", job.log)

    def test_reaping_leaves_a_live_job_alone(self):
        job = make_job()
        job.beat()
        self.assertEqual(SalesImportJob.reap_stale(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, SalesImportJob.Status.RUNNING)


class TriggerGuardTests(TestCase):
    """The guard that stops two runs overlapping must not stop ALL runs."""

    def post(self):
        return self.client.post(
            reverse("recipes:trigger_sales_import"),
            {"start_date": "2023-12-04", "end_date": "2026-09-03"},
            follow=True,
        )

    def test_a_dead_job_no_longer_blocks_a_new_run(self):
        """The deadlock: one killed thread locked this page out permanently."""
        age(make_job(), minutes=30)
        with mock.patch("recipes.views.threading.Thread") as thread:
            self.post()
        thread.assert_called_once()
        self.assertEqual(SalesImportJob.objects.filter(status="FAILED").count(), 1)

    def test_a_live_job_still_blocks(self):
        job = make_job()
        job.beat()
        with mock.patch("recipes.views.threading.Thread") as thread:
            response = self.post()
        thread.assert_not_called()
        self.assertTrue(any("déjà en cours" in str(m) for m in response.context["messages"]))

    def test_the_new_run_gets_the_dates_it_was_given(self):
        with mock.patch("recipes.views.threading.Thread") as thread:
            self.post()
        _args, kwargs = thread.call_args
        self.assertEqual(kwargs["args"][1], date(2023, 12, 4))
        self.assertEqual(kwargs["args"][2], date(2026, 9, 3))


class DownloadCancellationTests(TestCase):
    """Cancel used to be checked only either side of the download - which is
    the phase that actually takes minutes, so pressing it did nothing."""

    def test_the_wait_loop_asks_whether_it_should_stop(self):
        from recipes.pos.laddition_download import DownloadCancelled, _wait_for_new_xlsx

        calls = {"n": 0}

        def on_wait():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise DownloadCancelled()

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadCancelled):
                _wait_for_new_xlsx(directory, set(), on_wait=on_wait)
        self.assertEqual(calls["n"], 2)

    def test_cancelling_before_the_browser_starts_never_opens_one(self):
        from recipes.pos.laddition_download import DownloadCancelled, download_sales_lines

        with mock.patch("recipes.pos.laddition_download.laddition_session") as session:
            with self.assertRaises(DownloadCancelled):
                download_sales_lines(
                    date(2026, 1, 1), date(2026, 1, 31), "/tmp/x",
                    log=lambda *a: None, should_cancel=lambda: True,
                )
        session.assert_not_called()

    def test_a_cancelled_run_is_recorded_as_cancelled_not_failed(self):
        from recipes.tasks import import_laddition_sales_task

        job = make_job(status=SalesImportJob.Status.PENDING)
        from recipes.pos.laddition_download import DownloadCancelled

        with mock.patch(
            "recipes.tasks.download_sales_lines", side_effect=DownloadCancelled()
        ):
            import_laddition_sales_task(job.pk, date(2026, 1, 1), date(2026, 1, 31))
        job.refresh_from_db()
        self.assertEqual(job.status, SalesImportJob.Status.CANCELLED)


class HeartbeatTests(TestCase):
    def test_logging_counts_as_a_heartbeat(self):
        job = age(make_job(), minutes=30)
        self.assertTrue(job.is_stale)
        job.append_log("toujours là")
        self.assertFalse(job.is_stale)

    def test_beating_does_not_add_a_log_line(self):
        """The download has long silent stretches; a heartbeat every two
        seconds would bury the log it shares."""
        job = make_job()
        job.append_log("une ligne")
        before = job.log
        job.beat()
        job.refresh_from_db()
        self.assertEqual(job.log, before)
        self.assertIsNotNone(job.last_heartbeat)


class WriteBatchingTests(TestCase):
    """Three years of sales is a few thousand recipe/day totals. Written one
    autocommit transaction at a time against SQLite - while the status page
    polls the same file every second - one of those thousands of tiny writes
    eventually loses the race and the whole import dies with "database is
    locked", which is exactly what happened."""

    def test_every_row_is_still_written(self):
        from recipes.models import RecipeSale
        from recipes.sales import record_sales
        from tests.factories import make_recipe

        make_recipe(name="Mule")
        entries = [("Mule", date(2026, 1, 1) + timedelta(days=n), n + 1) for n in range(50)]
        result = record_sales(entries, source="test")

        self.assertEqual(result.created, 50)
        self.assertEqual(RecipeSale.objects.count(), 50)

    def test_a_failure_partway_leaves_nothing_half_written(self):
        """One transaction also means one outcome: the import either recorded
        the period or it didn't."""
        from recipes.models import RecipeSale
        from recipes.sales import record_sales
        from tests.factories import make_recipe

        make_recipe(name="Mule")
        entries = [("Mule", date(2026, 1, 1) + timedelta(days=n), n) for n in range(10)]

        with mock.patch(
            "recipes.models.RecipeSale.objects.update_or_create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                record_sales(entries, source="test")
        self.assertEqual(RecipeSale.objects.count(), 0)


class SqliteConcurrencyTests(TestCase):
    """The settings that let a background import and a polling status page
    share one SQLite file."""

    def test_the_write_timeout_is_generous(self):
        # The real database's options, not the in-memory one these tests run
        # against - that is the configuration the import actually meets.
        from config.settings import SQLITE_OPTIONS as options

        self.assertGreaterEqual(
            options.get("timeout", 5), 30,
            "SQLite's default 5s lock timeout is not enough for a multi-year import",
        )

    def test_wal_is_enabled(self):
        """WAL is what lets the status page keep reading while the import
        writes; without it the poller blocks the writer and vice versa."""
        from config.settings import SQLITE_OPTIONS

        init = SQLITE_OPTIONS.get("init_command", "")
        self.assertIn("WAL", init.upper())
