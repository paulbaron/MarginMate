"""The gathers said to be interrupted when the server starts.

Any Django process used to do it - `manage.py shell` too. On 02/09 a shell
opened to look at a gather marked it "Interrompu par un redémarrage du
serveur" seven seconds in, while its thread went on to sign in to Metro:
the run looked failed, the button came back, and the gather was run again
ten seconds later - into Metro's firewall, which had just refused it. Only
the process serving the pages, starting, has gathers of its own to reap.
"""

from unittest import mock

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from invoices.apps import serving_requests
from invoices.models import ScrapeJob


class ServingRequestsTests(TestCase):
    def test_the_dev_servers_serving_child_reaps(self):
        self.assertTrue(serving_requests(["manage.py", "runserver"], {"RUN_MAIN": "true"}))
        self.assertTrue(serving_requests(["manage.py", "runserver", "--noreload"], {}))

    def test_nothing_else_does(self):
        self.assertFalse(serving_requests(["manage.py", "shell"], {}))
        self.assertFalse(serving_requests(["manage.py", "check"], {}))
        # The autoreloader's parent only watches the files; its child serves.
        self.assertFalse(serving_requests(["manage.py", "runserver"], {}))

    def test_a_shell_starting_leaves_a_running_gather_alone(self):
        job = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        with mock.patch("invoices.apps.sys.argv", ["manage.py", "shell"]):
            apps.get_app_config("invoices").ready()
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.RUNNING)

    def test_the_server_starting_reaps_it(self):
        job = ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        with mock.patch("invoices.apps.sys.argv", ["manage.py", "runserver"]), mock.patch.dict(
            "invoices.apps.os.environ", {"RUN_MAIN": "true"}
        ):
            apps.get_app_config("invoices").ready()
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
