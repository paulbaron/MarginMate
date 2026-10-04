"""What the till import's job log may say of a failure (recipes/tasks.py).

The log is drawn on the « Ventes » page of whichever espace ran the job. A
hosted bar may run it (an export uploaded, its own L'Addition account), so
the log follows LB-3 like every other page: the till's own French refusals
as they are, anything else one fixed sentence - never a library's text,
which names the server's folders or the export's signed address. The
exception always reaches the server's log; the traceback is added to the
job's log in the server-accounts espace only (the owner's), as before.

Nothing is downloaded: the download is patched. Names and figures invented.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from selenium.common.exceptions import TimeoutException, WebDriverException

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from common import SERVER_ERROR
from recipes.models import SalesImportJob
from recipes.pos import laddition_download
from recipes.pos.laddition_download import LadditionDownloadError
from recipes.tasks import import_laddition_sales_task


class HostedJobLogTests(TwoTenantsTestCase):
    """Bar A is the owner's (server accounts), bar B a hosted bar."""

    owner_a = True

    def run_failing(self, tenant, error_for):
        """The job run in `tenant`, its download raising what `error_for`
        makes of the tenant's own downloads folder."""
        with bound_tenant(tenant):
            folder = paths.downloads_dir()
            job = SalesImportJob.objects.create()
            with (
                mock.patch("recipes.tasks.till_allowed", return_value=True),
                mock.patch("recipes.tasks.download_sales_lines", side_effect=error_for(folder)),
                self.assertLogs("recipes.tasks", "WARNING") as logged,
            ):
                import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30))
            job.refresh_from_db()
        return job, folder, logged

    @staticmethod
    def a_library_error(folder):
        return FileNotFoundError(2, "No such file or directory", str(Path(folder) / "export.xlsx"))

    def test_a_hosted_bar_s_failed_job_says_one_sentence_and_no_path(self):
        job, folder, logged = self.run_failing(self.bar_b, self.a_library_error)
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertIn(f"Échec : {SERVER_ERROR}", job.log)
        for leak in (str(folder), str(settings.TENANTS_ROOT), "Traceback", "No such file", "http"):
            self.assertNotIn(leak, job.log)
        # The detail is the server's: its log has it, traceback and all.
        self.assertIn("export.xlsx", "\n".join(logged.output))
        self.assertIn("Traceback", "\n".join(logged.output))

    def test_the_owner_s_job_still_carries_the_traceback(self):
        job, _folder, _logged = self.run_failing(self.bar_a, self.a_library_error)
        self.assertIn(f"Échec : {SERVER_ERROR}", job.log)
        self.assertIn("Traceback", job.log)

    def test_the_till_s_own_refusal_is_said_as_it_is(self):
        job, _folder, _logged = self.run_failing(
            self.bar_b, lambda folder: LadditionDownloadError(laddition_download.NO_ANSWER)
        )
        self.assertIn(f"Échec : {laddition_download.NO_ANSWER}", job.log)
        self.assertNotIn("Traceback", job.log)


class NothingDownloadedTests(TwoTenantsTestCase):
    owner_a = True

    def test_an_empty_download_is_the_till_s_refusal(self):
        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.create()
            with (
                mock.patch("recipes.tasks.till_allowed", return_value=True),
                mock.patch("recipes.tasks.download_sales_lines", return_value=[]),
                self.assertLogs("recipes.tasks", "WARNING"),
            ):
                import_laddition_sales_task(job.pk, date(2026, 6, 1), date(2026, 6, 30))
            job.refresh_from_db()
        self.assertIn("Échec : Aucun fichier téléchargé.", job.log)


class DownloadMessagesTests(SimpleTestCase):
    """L'Addition's download says what went wrong in French, with no folder
    and no address: the signed export URL opens the bar's sales to anyone."""

    def test_a_download_that_never_came_names_no_folder(self):
        folder = tempfile.mkdtemp(prefix="dossier-du-serveur-")
        with (
            mock.patch.object(laddition_download, "DOWNLOAD_TIMEOUT_SECONDS", 0),
            self.assertRaises(LadditionDownloadError) as caught,
        ):
            laddition_download._wait_for_new_xlsx(folder, set())
        self.assertNotIn(folder, str(caught.exception))
        self.assertIn("Aucun fichier .xlsx", str(caught.exception))

    def test_an_export_address_without_dates_is_not_repeated(self):
        driver = mock.Mock()
        signed = "https://exemple.invalid/export?db=add-0000&signature=abc123"
        driver.execute_script.side_effect = lambda script, *args: signed if "return window" in script else True
        with (
            mock.patch.object(laddition_download, "report_frame"),
            mock.patch.object(laddition_download, "WebDriverWait"),
            mock.patch.object(laddition_download.time, "sleep"),
            self.assertRaises(LadditionDownloadError) as caught,
        ):
            laddition_download.capture_export_template(driver, log=lambda *args: None)
        self.assertNotIn("signature", str(caught.exception))
        self.assertNotIn("https", str(caught.exception))

    def test_a_page_that_never_answered_is_one_french_sentence(self):
        for raised, said in (
            (TimeoutException("https://exemple.invalid/v2 timed out"), laddition_download.NO_ANSWER),
            (WebDriverException("net::ERR at https://exemple.invalid/"), laddition_download.NO_BROWSER),
        ):
            with self.subTest(raised=type(raised).__name__):
                with (
                    mock.patch.object(laddition_download, "laddition_session", side_effect=raised),
                    self.assertRaises(LadditionDownloadError) as caught,
                ):
                    laddition_download.download_sales_lines(
                        date(2026, 6, 1), date(2026, 6, 30), tempfile.mkdtemp(), log=lambda *args: None
                    )
                self.assertEqual(str(caught.exception), said)
