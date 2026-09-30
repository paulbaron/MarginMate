"""`manage.py running_jobs`: what deploy.cmd asks before it stops the
server (DEPLOY.md, section 10). Two espaces, each a temporary one; every job
is a row, nothing runs."""

from __future__ import annotations

import io
import tempfile
import warnings
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices.models import ReceiptBatch, ScrapeJob
from recipes.models import SalesImportJob


class RunningJobsTests(TwoTenantsTestCase):
    def run_it(self) -> tuple[str, str]:
        self.out, self.err = io.StringIO(), io.StringIO()
        call_command("running_jobs", stdout=self.out, stderr=self.err)
        return self.out.getvalue(), self.err.getvalue()

    def refused(self, returncode: int) -> str:
        with self.assertRaises(CommandError) as refusal:
            self.run_it()
        self.assertEqual(refusal.exception.returncode, returncode)
        return self.out.getvalue()

    def test_nothing_running(self):
        out, err = self.run_it()
        self.assertEqual(err, "")
        self.assertIn("Aucun travail en cours (2 espace(s) vérifié(s)).", out)

    def test_finished_jobs_do_not_count(self):
        with bound_tenant(self.bar_a):
            for status in (ScrapeJob.Status.SUCCESS, ScrapeJob.Status.FAILED, ScrapeJob.Status.CANCELLED):
                ScrapeJob.objects.create(status=status)
            ReceiptBatch.objects.create(status=ReceiptBatch.Status.SUCCESS)
        with bound_tenant(self.bar_b):
            SalesImportJob.objects.create(status=SalesImportJob.Status.FAILED)
        self.assertIn("Aucun travail en cours", self.run_it()[0])

    def test_every_kind_in_every_espace_is_named(self):
        now = timezone.now()
        with bound_tenant(self.bar_a):
            ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=now - timedelta(seconds=5))
        with bound_tenant(self.bar_b):
            ReceiptBatch.objects.create(status=ReceiptBatch.Status.PENDING)
            SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING, last_heartbeat=now)
            ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST, status=ScrapeJob.Status.RUNNING, last_heartbeat=now)
        out = self.refused(1)
        alpha = f"espace « Bar Alpha » ({self.bar_a.dir_name})"
        beta = f"espace « Bar Beta » ({self.bar_b.dir_name})"
        self.assertIn(f"- EN COURS : {alpha} : récupération des factures (en cours), commencée le ", out)
        self.assertIn(f"- EN COURS : {beta} : import de tickets et factures (en attente)", out)
        self.assertIn(f"- EN COURS : {beta} : import des ventes de la caisse (en cours)", out)
        self.assertIn(f"- EN COURS : {beta} : test d'une source de factures (en cours)", out)
        self.assertEqual(out.count("EN COURS"), 4)
        heard = timezone.localtime(now - timedelta(seconds=5)).strftime("%d/%m/%Y %H:%M:%S")
        self.assertIn(f"dernier signe de vie le {heard} (il y a ", out)
        self.assertNotIn("Aucun travail", out)

    def test_a_job_nothing_is_heard_from_does_not_count_and_is_left_as_it_is(self):
        """Presumed dead (the server restarted under it): the rule of
        transfer.runner.busy_reason, without its reaping - nothing is
        written."""
        with bound_tenant(self.bar_a):
            stale = ScrapeJob.objects.create(
                status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now() - timedelta(minutes=20)
            )
        out, _ = self.run_it()
        self.assertIn("- sans nouvelles : espace « Bar Alpha »", out)
        self.assertIn("(il y a 20 min) - considérée comme interrompue", out)
        self.assertIn("Aucun travail en cours", out)
        with bound_tenant(self.bar_a):
            stale.refresh_from_db()
        self.assertEqual(stale.status, ScrapeJob.Status.RUNNING)

    def test_an_espace_without_a_database_has_nothing_running(self):
        for leftover in paths.tenant_database(self.bar_b).parent.glob("db.sqlite3*"):
            leftover.unlink()
        out, _ = self.run_it()
        self.assertIn(f"espace « Bar Beta » ({self.bar_b.dir_name}) : pas de base, rien ne peut y tourner.", out)
        self.assertIn("Aucun travail en cours (1 espace(s) vérifié(s)).", out)

    def test_a_database_that_cannot_be_read_is_no_answer(self):
        paths.tenant_database(self.bar_b).write_bytes(b"ceci n'est pas une base SQLite " * 8)
        self.refused(2)
        self.assertIn(f"espace « Bar Beta » ({self.bar_b.dir_name}) : sa base ne se lit pas", self.err.getvalue())

    def test_a_closed_espace_that_cannot_be_read_blocks_nothing(self):
        """A closed espace (« actif » unticked: what serve and DEPLOY.md §11
        say to do with an espace whose base is gone) receives no request, so
        nothing can be started in it. Its unreadable base made the command
        exit 2, and deploy.cmd refused every deployment for it."""
        paths.tenant_database(self.bar_b).write_bytes(b"ceci n'est pas une base SQLite " * 8)
        type(self.bar_b).objects.filter(pk=self.bar_b.pk).update(is_active=False)
        out, err = self.run_it()
        self.assertEqual(err, "")
        self.assertIn(f"espace « Bar Beta » ({self.bar_b.dir_name}) : fermé, sa base ne se lit pas", out)
        self.assertIn("ignoré", out)
        self.assertIn("Aucun travail en cours (1 espace(s) vérifié(s)).", out)

    def test_a_closed_espace_with_a_bad_folder_name_blocks_nothing(self):
        type(self.bar_b).objects.filter(pk=self.bar_b.pk).update(is_active=False, dir_name="../dehors")
        out, err = self.run_it()
        self.assertEqual(err, "")
        self.assertIn("fermé, son nom de dossier est invalide", out)
        # Open, the same is no answer.
        type(self.bar_b).objects.filter(pk=self.bar_b.pk).update(is_active=True)
        self.refused(2)
        self.assertIn("son nom de dossier est invalide", self.err.getvalue())

    def test_a_running_job_wins_over_an_unreadable_espace(self):
        with bound_tenant(self.bar_a):
            ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        paths.tenant_database(self.bar_b).write_bytes(b"ceci n'est pas une base SQLite " * 8)
        self.refused(1)

    def test_a_missing_accounts_database_is_no_answer_and_is_not_made(self):
        missing = Path(tempfile.mkdtemp(prefix="marginmate-tests-jobs-")) / "comptes.sqlite3"
        databases = {**settings.DATABASES, "accounts": {**settings.DATABASES["accounts"], "NAME": str(missing)}}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with override_settings(DATABASES=databases), self.assertRaises(CommandError) as refusal:
                self.run_it()
        self.assertEqual(refusal.exception.returncode, 2)
        self.assertIn("La base des comptes est introuvable", str(refusal.exception))
        self.assertFalse(missing.exists())
