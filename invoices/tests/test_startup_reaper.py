"""The gathers said to be interrupted when the server starts.

Any Django process used to do it - `manage.py shell` too. On 02/09 a shell
opened to look at a gather marked it "Interrompu par un redémarrage du
serveur" seven seconds in, while its thread went on to sign in to Metro:
the run looked failed, the button came back, and the gather was run again
ten seconds later - into Metro's firewall, which had just refused it. Only
the process serving the pages, starting, has gathers of its own to reap.

And only the gathers nobody is running any more (security review PROD-2):
a runserver started beside the production server on the same data - the
debug recipe of DEPLOY.md, a preview refused its port - marked serve's
running gathers FAILED as its apps loaded. Now the reaping waits two
heartbeats in a thread of its own and takes only the jobs not heard from
since this process started.

The reaper's thread is never started for real here: its target is run in
the test's own thread, its wait patched out.
"""

import threading
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.db import connections
from django.test import TestCase
from django.utils import timezone

from invoices import apps as invoices_apps
from invoices.apps import REAP_DELAY_SECONDS, serving_requests
from invoices.models import ScrapeJob
from invoices.tasks import HEARTBEAT_SECONDS

INTERRUPTED = "Interrompu par un redémarrage du serveur."


def start_the_dev_server():
    """ready() as the dev server's serving child runs it. Returns the mock
    Thread class, holding what the reaper's thread was made with."""
    with (
        mock.patch("invoices.apps.sys.argv", ["manage.py", "runserver"]),
        mock.patch.dict("invoices.apps.os.environ", {"RUN_MAIN": "true"}),
        mock.patch("invoices.apps.threading.Thread") as thread,
    ):
        apps.get_app_config("invoices").ready()
    return thread


def run_the_reaper(thread, meanwhile=None):
    """Run the reaper's thread as started - in this thread, its wait
    replaced by `meanwhile(started)`: what other processes do during it."""
    target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
    started = args[0]
    with mock.patch("invoices.apps._wait", side_effect=lambda seconds: meanwhile and meanwhile(started)) as wait:
        target(*args)
    wait.assert_called_once_with(REAP_DELAY_SECONDS)


def gather(status=ScrapeJob.Status.RUNNING, heard=timedelta(minutes=1), **fields):
    """A gather whose last heartbeat was `heard` ago (None: never beat), and
    started before that."""
    job = ScrapeJob.objects.create(status=status, **fields)
    now = timezone.now()
    ScrapeJob.objects.filter(pk=job.pk).update(
        started_at=now - timedelta(minutes=5), last_heartbeat=None if heard is None else now - heard
    )
    job.refresh_from_db()
    return job


class ServingRequestsTests(TestCase):
    def test_the_dev_servers_serving_child_reaps(self):
        self.assertTrue(serving_requests(["manage.py", "runserver"], {"RUN_MAIN": "true"}))
        self.assertTrue(serving_requests(["manage.py", "runserver", "--noreload"], {}))

    def test_nothing_else_does(self):
        self.assertFalse(serving_requests(["manage.py", "shell"], {}))
        self.assertFalse(serving_requests(["manage.py", "check"], {}))
        # The autoreloader's parent only watches the files; its child serves.
        self.assertFalse(serving_requests(["manage.py", "runserver"], {}))

    def test_the_production_server_leaves_them_to_their_pages(self):
        """`serve` loads its apps before it knows it will serve: its
        --verifier, or a second `serve` refused the port, would reap the
        gathers of the server already running (invoices/apps.py)."""
        for argv in (
            ["manage.py", "serve"],
            ["manage.py", "serve", "--verifier"],
            ["manage.py", "serve", "--port", "8002"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(serving_requests(argv, {"RUN_MAIN": "true"}))

    def test_a_shell_starting_leaves_a_running_gather_alone(self):
        job = gather()
        with (
            mock.patch("invoices.apps.sys.argv", ["manage.py", "shell"]),
            mock.patch("invoices.apps.threading.Thread") as thread,
        ):
            apps.get_app_config("invoices").ready()
        thread.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.RUNNING)


class StartupReaperTests(TestCase):
    def test_the_server_starting_holds_nothing_up_and_reaps_nothing_yet(self):
        """As the apps load, nothing is read or written: the reaper is a
        daemon thread of its own (the server exits without waiting for it),
        started once."""
        dead = gather()
        thread = start_the_dev_server()
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["daemon"], True)
        thread.return_value.start.assert_called_once_with()
        dead.refresh_from_db()
        self.assertEqual(dead.status, ScrapeJob.Status.RUNNING)

    def test_the_server_starting_reaps_a_gather_its_dead_process_left(self):
        dead = gather()
        never_beat = gather(status=ScrapeJob.Status.PENDING, heard=None)
        run_the_reaper(start_the_dev_server())
        for job in (dead, never_beat):
            job.refresh_from_db()
            self.assertEqual(job.status, ScrapeJob.Status.FAILED)
            self.assertIsNotNone(job.finished_at)
            self.assertIn(INTERRUPTED, job.log)

    def test_a_gather_another_live_server_runs_is_left_running(self):
        """`serve` runs a gather; a runserver starts on the same data. The
        gather beat 5 s before - and beats again during the wait, as a live
        one does every HEARTBEAT_SECONDS: it is not reaped, its page says
        nothing false, the Gather button stays taken."""
        live = gather(heard=timedelta(seconds=5), log="[+   3.0s] Connexion à la boîte mail…\n")

        def serve_beats(started):
            ScrapeJob.objects.filter(pk=live.pk).update(last_heartbeat=started + timedelta(seconds=HEARTBEAT_SECONDS))

        run_the_reaper(start_the_dev_server(), meanwhile=serve_beats)
        live.refresh_from_db()
        self.assertEqual(live.status, ScrapeJob.Status.RUNNING)
        self.assertIsNone(live.finished_at)
        self.assertNotIn(INTERRUPTED, live.log)

    def test_a_gather_started_during_the_wait_is_left_alone(self):
        """This server's own gather, asked for before the reaper woke up."""
        thread = start_the_dev_server()
        started = thread.call_args.kwargs["args"][0]
        mine = []

        def someone_gathers(started):
            job = ScrapeJob.objects.create(status=ScrapeJob.Status.PENDING)
            ScrapeJob.objects.filter(pk=job.pk).update(started_at=started + timedelta(seconds=10))
            mine.append(job)

        run_the_reaper(thread, meanwhile=someone_gathers)
        (job,) = mine
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.PENDING)
        self.assertLess(started, job.started_at)

    def test_a_finished_gather_is_never_touched(self):
        done = gather(status=ScrapeJob.Status.SUCCESS)
        before = (done.status, done.finished_at, done.log)
        run_the_reaper(start_the_dev_server())
        done.refresh_from_db()
        self.assertEqual((done.status, done.finished_at, done.log), before)


class ReaperThreadTests(TestCase):
    def test_it_waits_two_heartbeats_and_far_less_than_the_page_s_reaper(self):
        """Two beats of a live gather and a margin - and a dead one is still
        reaped long before its page would (STALE_AFTER, 10 minutes)."""
        self.assertGreaterEqual(REAP_DELAY_SECONDS, 2 * HEARTBEAT_SECONDS)
        self.assertLessEqual(REAP_DELAY_SECONDS, 3 * HEARTBEAT_SECONDS)
        self.assertLess(REAP_DELAY_SECONDS, ScrapeJob.STALE_AFTER.total_seconds())

    def test_its_own_thread_closes_its_connections_and_only_its_own(self):
        with (
            mock.patch("invoices.apps._wait"),
            mock.patch("invoices.apps.reap_interrupted_gathers"),
            mock.patch.object(connections, "close_all") as close_all,
        ):
            invoices_apps._reap_after_delay(timezone.now(), threading.get_ident())
            close_all.assert_not_called()
            invoices_apps._reap_after_delay(timezone.now(), threading.get_ident() + 1)
            close_all.assert_called_once_with()

    def test_a_reaper_that_fails_says_so_in_the_log_and_raises_nothing(self):
        with (
            mock.patch("invoices.apps._wait"),
            mock.patch("invoices.apps.reap_interrupted_gathers", side_effect=RuntimeError("base verrouillée")),
            self.assertLogs("invoices.apps", "ERROR") as logged,
        ):
            invoices_apps._reap_after_delay(timezone.now(), threading.get_ident())
        self.assertIn("nettoyage des récupérations interrompues", logged.output[0])
