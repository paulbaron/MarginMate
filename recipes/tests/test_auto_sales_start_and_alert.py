"""One sales import at a time, whoever starts it (recipes/importing.py:
`start_sales_import`, shared by the Ventes tab and the automatic imports),
and how an automatic import ends (auto_sales.finished: the
« recipes-auto-sales » alert, its outcomes, a failure said once a day and
never with its traceback or an address, nothing for a cancelled one).

The import's thread is patched; nothing is downloaded. Data invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notifications import registry
from notifications.models import Dispatch, EventRule
from notifications.tests.support import QuietLogs
from recipes import auto_sales, importing
from recipes.models import AutoSalesImport, SalesImportJob
from recipes.pos.laddition_download import DownloadCancelled
from recipes.tasks import import_laddition_sales_task

JUNE = (date(2026, 6, 1), date(2026, 6, 30))


class StartTests(TestCase):
    def start(self, **kwargs):
        with mock.patch("recipes.importing.threading.Thread") as thread:
            job = importing.start_sales_import(*JUNE, **kwargs)
        return job, thread

    def test_it_creates_the_job_and_starts_its_bound_thread(self):
        job, thread = self.start(trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=7)
        self.assertEqual((job.range_start, job.range_end), JUNE)
        self.assertEqual((job.trigger, job.auto_rule_id), (SalesImportJob.Trigger.AUTOMATIC, 7))
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, import_laddition_sales_task)
        self.assertEqual(thread.call_args.kwargs["args"], (job.pk, *JUNE))
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    def test_a_manual_import_by_default(self):
        job, _thread = self.start()
        self.assertEqual(job.trigger, SalesImportJob.Trigger.MANUAL)
        self.assertIsNone(job.auto_rule_id)

    def test_none_while_another_runs_and_nothing_created(self):
        running = SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        running.beat()
        job, thread = self.start()
        self.assertIsNone(job)
        thread.assert_not_called()
        self.assertEqual(SalesImportJob.objects.count(), 1)

    def test_a_dead_run_is_reaped_first(self):
        dead = SalesImportJob.objects.create(status=SalesImportJob.Status.RUNNING)
        SalesImportJob.objects.filter(pk=dead.pk).update(started_at=timezone.now() - timedelta(minutes=30))
        job, _thread = self.start()
        self.assertIsNotNone(job)
        dead.refresh_from_db()
        self.assertEqual(dead.status, SalesImportJob.Status.FAILED)

    def test_the_check_and_the_creation_share_one_transaction(self):
        """IMMEDIATE takes the write lock as the block opens: a click and the
        scheduler arriving together cannot both see « nothing running »."""
        depths = []
        real_check = importing.active_import
        real_create = SalesImportJob.objects.create

        def check(*args, **kwargs):
            depths.append(("check", len(connection.savepoint_ids)))
            return real_check(*args, **kwargs)

        def create(**kwargs):
            depths.append(("create", len(connection.savepoint_ids)))
            return real_create(**kwargs)

        outside = len(connection.savepoint_ids)
        with (
            mock.patch("recipes.importing.active_import", side_effect=check),
            mock.patch.object(SalesImportJob.objects, "create", side_effect=create),
            mock.patch("recipes.importing.threading.Thread") as thread,
        ):
            importing.start_sales_import(*JUNE)
        self.assertEqual(depths, [("check", outside + 1), ("create", outside + 1)])
        thread.return_value.start.assert_called_once()

    def test_notes_open_the_log(self):
        job, _thread = self.start(notes=["Début ramené au 01/06/2026."])
        self.assertIn("Début ramené au 01/06/2026.", job.log)

    def test_the_tab_refuses_while_an_automatic_import_runs(self):
        running = SalesImportJob.objects.create(
            status=SalesImportJob.Status.RUNNING, trigger=SalesImportJob.Trigger.AUTOMATIC
        )
        running.beat()
        with mock.patch("recipes.views.threading.Thread") as thread:
            response = self.client.post(
                reverse("recipes:trigger_sales_import"),
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
                follow=True,
            )
        thread.assert_not_called()
        self.assertContains(response, "Une récupération est déjà en cours.")
        # The tab shows the automatic import running, its form held.
        self.assertContains(response, "Lancé automatiquement")
        self.assertContains(response, 'data-job-control="sales-import-status" disabled')

    def test_the_tab_refuses_an_import_started_between_its_check_and_its_start(self):
        with (
            mock.patch("recipes.views.start_sales_import", return_value=None),
            mock.patch("recipes.views.threading.Thread") as thread,
        ):
            response = self.client.post(
                reverse("recipes:trigger_sales_import"),
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
                follow=True,
            )
        thread.assert_not_called()
        self.assertContains(response, "Une récupération est déjà en cours.")

    def test_the_tab_links_to_the_settings(self):
        self.assertContains(
            self.client.get(reverse("recipes:sales_list")),
            f'<a href="{reverse("recipes:auto_sales")}">Import automatique : réglages</a>',
        )


class AlertTests(QuietLogs, TestCase):
    def setUp(self):
        super().setUp()
        self.rule = AutoSalesImport.objects.create(name="Ventes exemple", weekdays="0,1,2,3,4,5,6", times="07:00")

    def job(self, status, **fields) -> SalesImportJob:
        values = {
            "status": status,
            "trigger": SalesImportJob.Trigger.AUTOMATIC,
            "auto_rule_id": self.rule.pk,
            "range_start": date(2026, 6, 1),
            "range_end": date(2026, 6, 3),
            "finished_at": timezone.now(),
            **fields,
        }
        return SalesImportJob.objects.create(**values)

    def finished(self, job, error=""):
        with mock.patch("notifications.events.emit") as emit:
            auto_sales.finished(job, source_key="laddition", own=None, error=error)
        return emit

    def test_new_when_day_totals_were_recorded(self):
        emit = self.finished(self.job(SalesImportJob.Status.SUCCESS, recorded=12, unmatched=3))
        args, kwargs = emit.call_args
        self.assertEqual(args, (registry.RECIPES_AUTO_SALES, "new"))
        self.assertEqual(kwargs["title"], "Import automatique des ventes : ventes importées")
        self.assertEqual(
            kwargs["body"],
            "Ventes du 01/06 au 03/06 : 12 totaux recette/jour · 3 produits de caisse sans recette",
        )
        self.assertEqual(kwargs["target"], reverse("recipes:sales_list"))
        self.assertTrue(kwargs["content_key"].startswith("sales:"))

    def test_nothing_when_none_was(self):
        job = self.job(SalesImportJob.Status.SUCCESS, recorded=0)
        emit = self.finished(job)
        self.assertEqual(emit.call_args.args[1], "nothing")
        self.assertEqual(emit.call_args.kwargs["content_key"], f"sales:{job.pk}")

    def test_failed_once_a_day_per_rule(self):
        error = (
            "Délai dépassé sur https://app.laddition.exemple/export?jeton=abc123\nTraceback (most recent call last):"
        )
        emit = self.finished(self.job(SalesImportJob.Status.FAILED), error=error)
        args, kwargs = emit.call_args
        self.assertEqual(args[1], "failed")
        day = timezone.localdate()
        self.assertEqual(kwargs["content_key"], f"sales-failed:{self.rule.pk}:{day.isoformat()}")
        self.assertEqual(kwargs["body"], "Échec : Délai dépassé sur (adresse masquée)")
        self.assertNotIn("Traceback", kwargs["body"])
        self.assertNotIn("jeton", kwargs["body"])
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.last_failed, day)
        tomorrow = self.finished(
            self.job(SalesImportJob.Status.FAILED, finished_at=timezone.now() + timedelta(days=1)), error="Panne"
        )
        self.assertNotEqual(tomorrow.call_args.kwargs["content_key"], kwargs["content_key"])

    def test_a_success_clears_the_last_failure(self):
        AutoSalesImport.objects.filter(pk=self.rule.pk).update(last_failed=date(2026, 6, 2))
        self.finished(self.job(SalesImportJob.Status.SUCCESS, recorded=1))
        self.rule.refresh_from_db()
        self.assertIsNone(self.rule.last_failed)

    def test_a_cancelled_import_says_nothing(self):
        self.finished(self.job(SalesImportJob.Status.CANCELLED)).assert_not_called()

    def test_a_manual_import_says_nothing(self):
        emit = self.finished(self.job(SalesImportJob.Status.SUCCESS, trigger=SalesImportJob.Trigger.MANUAL))
        emit.assert_not_called()

    def test_two_failures_the_same_day_make_one_dispatch(self):
        EventRule.objects.create(event=registry.RECIPES_AUTO_SALES, outcomes=["failed"], recipient_ids=[5])
        with mock.patch("notifications.events.threading.Thread"):
            for _ in range(2):
                with self.captureOnCommitCallbacks(execute=True):
                    auto_sales.finished(self.job(SalesImportJob.Status.FAILED), source_key="laddition", own=None)
        dispatch = Dispatch.objects.get()
        self.assertEqual((dispatch.event, dispatch.outcome), (registry.RECIPES_AUTO_SALES, "failed"))
        self.assertEqual(dispatch.body, "Échec : erreur inconnue")

    def test_an_alert_going_wrong_never_raises(self):
        with (
            mock.patch("notifications.events.emit", side_effect=RuntimeError("panne")),
            self.assertLogs("recipes.auto_sales", "ERROR") as logs,
        ):
            auto_sales.finished(self.job(SalesImportJob.Status.SUCCESS), source_key="laddition", own=None)
        self.assertIn("fin non signalée", "\n".join(logs.output))

    def run_task(self, *, download):
        job = SalesImportJob.objects.create(
            range_start=JUNE[0], range_end=JUNE[1], trigger=SalesImportJob.Trigger.AUTOMATIC, auto_rule_id=self.rule.pk
        )
        with (
            mock.patch("recipes.tasks.download_sales_lines", **download),
            mock.patch("notifications.events.emit") as emit,
        ):
            import_laddition_sales_task(job.pk, *JUNE, download_dir="non-utilisé")
        job.refresh_from_db()
        return job, emit

    def test_the_task_alerts_on_a_failure_with_the_error_s_first_line(self):
        job, emit = self.run_task(download={"side_effect": RuntimeError("Identifiants refusés par la caisse.")})
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertEqual(emit.call_args.kwargs["body"], "Échec : Identifiants refusés par la caisse.")

    def test_the_task_says_nothing_when_cancelled(self):
        job, emit = self.run_task(download={"side_effect": DownloadCancelled()})
        self.assertEqual(job.status, SalesImportJob.Status.CANCELLED)
        emit.assert_not_called()

    def test_the_task_refused_in_another_espace_says_nothing(self):
        with mock.patch("recipes.tasks.till_allowed", return_value=False):
            job, emit = self.run_task(download={"return_value": []})
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        emit.assert_not_called()


class RegistryTests(TestCase):
    def test_the_event(self):
        event = registry.EVENTS["recipes-auto-sales"]
        self.assertEqual(event.label, "Import automatique des ventes")
        self.assertEqual(
            [(outcome.key, outcome.label) for outcome in event.outcomes],
            [("new", "ventes importées"), ("failed", "échec"), ("nothing", "rien de nouveau")],
        )
        self.assertEqual(event.default_outcomes, ("failed",))

    def test_the_alerts_page_draws_its_card(self):
        response = self.client.get(reverse("notifications:events"))
        self.assertContains(response, 'id="alerte-recipes-auto-sales"')
        self.assertContains(response, "Un échec n'est signalé qu'une fois par jour.")
