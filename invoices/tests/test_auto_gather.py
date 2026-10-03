"""Automatic gathers (invoices/auto_gather.py, tasks.gather_invoices_task's
`unattended` and its end-of-run alert, workspace.gather_sources) and the
manual-only reading of Achats and Consignes.

No gather ever runs for real: `invoices.gathering.threading.Thread` is
patched wherever a slot could start one, and the task's mailbox, portal and
Metro calls are replaced. Every name and date is invented; the clock is the
`now` handed to run_due - a Wednesday in October (CEST, UTC+2).
"""

import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string

from invoices import auto_gather, workspace
from invoices.models import AutoGather, EmailInvoiceSource, InvoiceType, ScrapeJob, WebsiteInvoiceSource
from invoices.tasks import DEFAULT_LOOKBACK_DAYS, gather_invoices_task
from returnables.models import SlipFormat
from returnables.tests.support import SEEDED_FORMAT_NAME
from tests.factories import make_supplier

#: Wednesday 07/10/2026, 06:00 in Paris.
WEDNESDAY_6AM = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
WEDNESDAY = 2


def email_type(supplier, name="Cave Exemple - Factures"):
    invoice_type = InvoiceType.objects.create(supplier=supplier, name=name, source_kind=InvoiceType.SourceKind.EMAIL)
    EmailInvoiceSource.objects.create(invoice_type=invoice_type, sender_pattern="factures@cave.exemple")
    return invoice_type


def website_type(supplier, name="Box Exemple - Factures", **settings):
    invoice_type = InvoiceType.objects.create(supplier=supplier, name=name, source_kind=InvoiceType.SourceKind.WEBSITE)
    WebsiteInvoiceSource.objects.create(
        invoice_type=invoice_type,
        login_url="https://box.exemple.fr/login",
        username_env="BOX_LOGIN",
        password_env="BOX_PASSWORD",
        **settings,
    )
    return invoice_type


def slips_code() -> str:
    return f"bons-{SlipFormat.objects.get(name=SEEDED_FORMAT_NAME).pk}"


class Sources:
    def make_sources(self):
        self.cave = email_type(make_supplier(code="CAVE_X", name="Cave Exemple"))
        self.box = website_type(make_supplier(code="BOX_X", name="Box Exemple", expenses_only=True))
        self.email_code = f"type-{self.cave.pk}"
        self.portal_code = f"type-{self.box.pk}"
        self.slips_code = slips_code()


class RunDueCase(Sources, TestCase):
    def setUp(self):
        self.make_sources()
        self.base_dir = tempfile.mkdtemp()
        settings = override_settings(BASE_DIR=Path(self.base_dir))
        settings.enable()
        self.addCleanup(settings.disable)

    def rule(self, **fields) -> AutoGather:
        values = {
            "name": "Bons exemple",
            "sources": [self.slips_code],
            "weekdays": str(WEDNESDAY),
            "start_time": time(6, 0),
            "end_time": time(14, 0),
            "every_minutes": 30,
            "created_at": WEDNESDAY_6AM - timedelta(days=7),
            **fields,
        }
        return AutoGather.objects.create(**values)

    def run_due(self, now, *, enabled=True):
        with (
            mock.patch("notifications.webpush.sending_enabled", return_value=enabled),
            mock.patch("invoices.gathering.threading.Thread") as thread,
        ):
            auto_gather.run_due(now)
        return thread


class SlotTests(RunDueCase):
    def test_the_due_slot_is_the_latest_of_the_window(self):
        rule = self.rule()
        self.assertEqual(
            auto_gather.due_slot(rule, WEDNESDAY_6AM + timedelta(minutes=70)), WEDNESDAY_6AM + timedelta(hours=1)
        )

    def test_a_day_not_ticked_has_no_slot(self):
        rule = self.rule(weekdays="0,4", created_at=WEDNESDAY_6AM - timedelta(hours=1))
        self.assertIsNone(auto_gather.due_slot(rule, WEDNESDAY_6AM + timedelta(minutes=5)))

    def test_a_rule_created_after_the_slot_does_not_run_it(self):
        rule = self.rule(created_at=WEDNESDAY_6AM + timedelta(minutes=10))
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=20))
        thread.assert_not_called()
        rule.refresh_from_db()
        self.assertIsNone(rule.last_slot_at)

    def test_the_slot_after_the_last_one_taken(self):
        rule = self.rule(last_slot_at=WEDNESDAY_6AM)
        self.assertIsNone(auto_gather.due_slot(rule, WEDNESDAY_6AM + timedelta(minutes=29)))
        self.assertEqual(
            auto_gather.due_slot(rule, WEDNESDAY_6AM + timedelta(minutes=31)), WEDNESDAY_6AM + timedelta(minutes=30)
        )

    def test_the_scheduler_finds_the_job_by_its_name(self):
        self.assertIs(import_string("invoices.auto_gather.run_due"), auto_gather.run_due)


class ClaimTests(RunDueCase):
    def test_a_slot_is_taken_once_by_two_ticks(self):
        rule = self.rule()
        first = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        ScrapeJob.objects.update(status=ScrapeJob.Status.SUCCESS)
        second = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        first.return_value.start.assert_called_once()
        second.assert_not_called()
        rule.refresh_from_db()
        self.assertEqual(rule.last_slot_at, WEDNESDAY_6AM)

    def test_the_claim_is_conditional_on_what_was_read(self):
        self.rule()
        mine, theirs = AutoGather.objects.get(), AutoGather.objects.get()
        self.assertTrue(auto_gather.claim(mine, WEDNESDAY_6AM))
        self.assertFalse(auto_gather.claim(theirs, WEDNESDAY_6AM))

    def test_a_claim_lost_starts_nothing(self):
        rule = self.rule()
        with mock.patch("invoices.auto_gather.claim", return_value=False):
            thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        rule.refresh_from_db()
        self.assertEqual(rule.last_result, "")


class SkipTests(RunDueCase):
    def result(self, rule) -> str:
        rule.refresh_from_db()
        return rule.last_result

    def test_a_development_server_never_gathers_and_says_so(self):
        rule = self.rule()
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1), enabled=False)
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : serveur de développement")
        self.assertEqual(rule.last_slot_at, WEDNESDAY_6AM)

    def test_the_real_check_says_a_test_server_is_a_development_one(self):
        rule = self.rule()
        with mock.patch("invoices.gathering.threading.Thread") as thread:
            auto_gather.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), auto_gather.DEV_SERVER)

    def test_a_slot_older_than_its_period_is_missed_not_caught_up(self):
        # Several times a day (06:00 and 06:30): caught up within its 30 min.
        rule = self.rule(end_time=time(6, 30))
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=61))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "manquée : serveur arrêté à 06:30")
        self.assertEqual(rule.last_slot_at, WEDNESDAY_6AM + timedelta(minutes=30))

    def test_a_slot_younger_than_its_period_is_caught_up_once(self):
        rule = self.rule(end_time=time(6, 0))
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=29))
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.result(rule).startswith("lancée à 06:29"))

    def test_the_catch_up_never_reaches_past_two_hours(self):
        # 06:00 and 18:00: twice a day, so min(12 h, 2 h).
        rule = self.rule(end_time=time(18, 0), every_minutes=720)
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=119))
        thread.return_value.start.assert_called_once()
        ScrapeJob.objects.update(status=ScrapeJob.Status.SUCCESS)
        AutoGather.objects.filter(pk=rule.pk).update(last_slot_at=None)
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=120))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "manquée : serveur arrêté à 06:00")

    def test_a_deploy_under_way_skips_the_slot(self):
        (Path(self.base_dir) / ".git" / "marginmate-deploy").mkdir(parents=True)
        rule = self.rule()
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : mise à jour du site en cours")
        self.assertEqual(rule.last_slot_at, WEDNESDAY_6AM)

    def test_a_gather_already_running_makes_the_slot_wait(self):
        ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        rule = self.rule()
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "en attente : une récupération est en cours")
        # The slot is given back: the next tick tries it again.
        self.assertIsNone(rule.last_slot_at)
        self.assertEqual(auto_gather.due_slot(rule, WEDNESDAY_6AM + timedelta(minutes=2)), WEDNESDAY_6AM)
        self.assertEqual(ScrapeJob.objects.count(), 1)

    def test_metro_and_a_portal_alone_leave_no_source(self):
        rule = self.rule(sources=["METRO", self.portal_code])
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        self.assertEqual(self.result(rule), "sautée : aucune source disponible")

    def test_a_source_switched_off_since_is_dropped(self):
        rule = self.rule(sources=[self.email_code])
        InvoiceType.objects.filter(pk=self.cave.pk).update(is_active=False)
        self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        self.assertEqual(self.result(rule), auto_gather.NO_SOURCE)

    def test_an_inactive_rule_is_left_alone(self):
        rule = self.rule(is_active=False)
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        rule.refresh_from_db()
        self.assertIsNone(rule.last_slot_at)

    def test_a_rule_whose_days_cannot_be_read_is_skipped_and_said(self):
        broken = self.rule(weekdays="9")
        fine = self.rule(name="Autre", sources=[self.email_code])
        with self.assertLogs("invoices.auto_gather", "WARNING"):
            thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.return_value.start.assert_called_once()
        self.assertEqual(self.result(broken), "sautée : jours ou heures illisibles")
        self.assertTrue(self.result(fine).startswith("lancée"))

    def test_nothing_where_the_servers_accounts_may_not_be_used(self):
        rule = self.rule()
        with mock.patch("invoices.auto_gather.integrations_allowed", return_value=False):
            thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        thread.assert_not_called()
        rule.refresh_from_db()
        self.assertIsNone(rule.last_slot_at)


class LaunchTests(RunDueCase):
    def test_a_slot_starts_an_unattended_automatic_gather_of_its_mailbox_sources(self):
        rule = self.rule(sources=["METRO", self.portal_code, self.email_code, self.slips_code])
        thread = self.run_due(WEDNESDAY_6AM + timedelta(minutes=1))
        job = ScrapeJob.objects.get()
        self.assertEqual((job.trigger, job.auto_gather_id), (ScrapeJob.Trigger.AUTOMATIC, rule.pk))
        self.assertEqual((job.range_start, job.range_end), (None, None))
        call = thread.call_args.kwargs
        self.assertEqual(call["args"], (job.id, None, None, {self.email_code, self.slips_code}, False))
        self.assertEqual(call["kwargs"], {"unattended": True})
        thread.return_value.start.assert_called_once()
        rule.refresh_from_db()
        self.assertEqual(rule.last_result, f"lancée à 06:01 (récupération n° {job.pk})")
        self.assertEqual(rule.last_slot_at, WEDNESDAY_6AM)


class PruneTests(RunDueCase):
    def job(self, trigger, age_days, status=ScrapeJob.Status.SUCCESS):
        job = ScrapeJob.objects.create(trigger=trigger, status=status, last_heartbeat=WEDNESDAY_6AM)
        ScrapeJob.objects.filter(pk=job.pk).update(started_at=WEDNESDAY_6AM - timedelta(days=age_days))
        return job.pk

    def test_automatic_runs_older_than_30_days_go(self):
        old_auto = self.job(ScrapeJob.Trigger.AUTOMATIC, 31)
        recent_auto = self.job(ScrapeJob.Trigger.AUTOMATIC, 29)
        old_manual = self.job(ScrapeJob.Trigger.MANUAL, 200)
        self.run_due(WEDNESDAY_6AM)
        kept = set(ScrapeJob.objects.values_list("pk", flat=True))
        self.assertNotIn(old_auto, kept)
        self.assertEqual(kept, {recent_auto, old_manual})

    def test_a_running_one_is_never_pruned(self):
        running = self.job(ScrapeJob.Trigger.AUTOMATIC, 40, status=ScrapeJob.Status.RUNNING)
        self.assertEqual(auto_gather.prune(WEDNESDAY_6AM), 0)
        self.assertTrue(ScrapeJob.objects.filter(pk=running).exists())


class GatherSourcesTests(Sources, TestCase):
    def setUp(self):
        self.make_sources()

    def test_each_source_says_its_kind(self):
        sources, gathered = workspace.gather_sources()
        kinds = {source["code"]: source["kind"] for source in sources}
        self.assertEqual(kinds["METRO"], "metro")
        self.assertEqual(kinds[self.email_code], "email")
        self.assertEqual(kinds[self.portal_code], "portal")
        self.assertEqual(kinds[self.slips_code], "slips")
        self.assertNotIn("allowed", sources[0])
        self.assertIn(self.cave.supplier_id, gathered)

    def test_an_automatic_gather_takes_the_mailbox_only(self):
        sources, _ = workspace.gather_sources(for_auto=True)
        by_code = {source["code"]: source for source in sources}
        self.assertFalse(by_code["METRO"]["allowed"])
        self.assertIn("pare-feu", by_code["METRO"]["reason"])
        self.assertFalse(by_code[self.portal_code]["allowed"])
        self.assertIn("code par SMS", by_code[self.portal_code]["reason"])
        self.assertTrue(by_code[self.email_code]["allowed"])
        self.assertTrue(by_code[self.slips_code]["allowed"])
        self.assertEqual(by_code[self.email_code]["reason"], "")

    def test_none_where_the_servers_accounts_may_not_be_used(self):
        with (
            mock.patch("invoices.workspace.integrations_allowed", return_value=False),
            mock.patch("invoices.workspace.own_module_suppliers", return_value=InvoiceType.objects.none()),
        ):
            self.assertEqual(workspace.gather_sources(for_auto=True), ([], set()))


def old_gather_sources():
    """workspace._import_card's sources as written before gather_sources was
    factored out of it (main c54af83), for the comparison below."""
    from invoices.tasks import slips_code as code_of
    from invoices.tasks import slips_label

    allowed = workspace.integrations_allowed()
    metro = workspace.own_module_suppliers().first()
    email_types = list(InvoiceType.objects.filter(is_active=True).select_related("supplier")) if allowed else []
    gather_sources = []
    if metro:
        from invoices.scrapers.metro import metro_pause

        gather_sources.append({"code": "METRO", "label": metro.name, "paused": metro_pause()})
    gather_sources += [{"code": f"type-{it.id}", "label": it.name} for it in email_types]
    if allowed:
        gather_sources += [
            {"code": code_of(fmt), "label": slips_label(fmt)}
            for fmt in SlipFormat.objects.filter(is_active=True).exclude(sender_pattern="").order_by("name", "pk")
        ]
    gathered = {it.supplier_id for it in email_types}
    if metro:
        gathered.add(metro.pk)
    return gather_sources, gathered


class ImportCardUnchangedTests(Sources, TestCase):
    """The card draws what it drew before gather_sources was factored out."""

    def setUp(self):
        self.make_sources()
        InvoiceType.objects.create(supplier=self.cave.supplier, name="Cave Exemple - Ancien", is_active=False)

    def card(self):
        return self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer").context

    def comparable(self, sources):
        """Metro's pause as its sentence: an exception is equal to itself only."""
        return [{key: str(value) if key == "paused" else value for key, value in source.items()} for source in sources]

    def test_the_same_sources_in_the_same_order(self):
        expected, gathered = old_gather_sources()
        context = self.card()
        # Key for key: no « kind » on the card's entries (gather_sources' own).
        self.assertEqual(self.comparable(context["gather_sources"]), self.comparable(expected))
        self.assertEqual(workspace.gather_sources()[1], gathered)

    def test_a_paused_metro_keeps_its_pause_on_the_card(self):
        from invoices.scrapers.metro import record_block

        record_block("#18.0000000.1700000000.00000abc")
        expected, _ = old_gather_sources()
        context = self.card()
        self.assertEqual(self.comparable(context["gather_sources"]), self.comparable(expected))
        self.assertTrue(context["gather_sources"][0]["paused"])

    def test_nothing_in_a_tenant_without_the_servers_accounts(self):
        with (
            mock.patch("invoices.workspace.integrations_allowed", return_value=False),
            mock.patch("invoices.workspace.own_module_suppliers", return_value=InvoiceType.objects.none()),
        ):
            expected, _ = old_gather_sources()
            context = self.card()
        self.assertEqual(expected, [])
        self.assertEqual(context["gather_sources"], [])

    def test_the_card_links_to_the_automatic_gathers(self):
        page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertContains(page, f'href="{reverse("invoices:auto_gathers")}"')
        self.assertContains(page, "Récupération automatique : réglages")


def gather_job(status, trigger=ScrapeJob.Trigger.MANUAL, **fields):
    return ScrapeJob.objects.create(kind=ScrapeJob.Kind.GATHER, status=status, trigger=trigger, **fields)


class ManualOnlyOnAchatsTests(TestCase):
    """Achats' period, « missed » sources and latest run are a person's
    gathers; an automatic one running is shown all the same."""

    def card(self):
        return self.client.get(reverse("invoices:invoice_list")).context

    def test_a_failed_automatic_run_does_not_hold_the_period(self):
        gather_job(ScrapeJob.Status.SUCCESS, range_start=date(2026, 9, 1), range_end=date(2026, 9, 18), progress={})
        gather_job(
            ScrapeJob.Status.FAILED,
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            range_start=date(2026, 1, 1),
            progress={"type-1": {"label": "Cave", "error": "Boîte mail : refusée"}},
        )
        context = self.card()
        self.assertNotEqual(context["default_start_date"], date(2026, 1, 1))
        self.assertEqual(context["latest_job"].trigger, ScrapeJob.Trigger.MANUAL)

    def test_a_manual_failure_is_offered_again_beside_a_later_automatic_run(self):
        missed = {"type-1": {"label": "Cave", "error": "Boîte mail : refusée"}}
        gather_job(ScrapeJob.Status.SUCCESS, range_start=date(2026, 3, 1), progress=missed)
        gather_job(
            ScrapeJob.Status.SUCCESS, trigger=ScrapeJob.Trigger.AUTOMATIC, range_start=date(2026, 3, 1), progress={}
        )
        self.assertEqual(self.card()["default_start_date"], date(2026, 3, 1))

    def test_an_automatic_run_is_not_the_previous_one_missed_again(self):
        missed = {"type-1": {"label": "Cave", "error": "Boîte mail : refusée"}}
        earlier = gather_job(
            ScrapeJob.Status.SUCCESS, trigger=ScrapeJob.Trigger.AUTOMATIC, range_start=date(2026, 3, 1), progress=missed
        )
        latest = gather_job(ScrapeJob.Status.SUCCESS, range_start=date(2026, 3, 1), progress=missed)
        ScrapeJob.objects.filter(pk=earlier.pk).update(started_at=timezone.now() - timedelta(hours=1))
        self.assertFalse(workspace._missed_again(latest))
        self.assertEqual(self.card()["default_start_date"], date(2026, 3, 1))

    def test_an_automatic_gather_running_is_shown_as_running(self):
        gather_job(ScrapeJob.Status.SUCCESS, range_start=date(2026, 9, 1), progress={})
        running = gather_job(
            ScrapeJob.Status.RUNNING, trigger=ScrapeJob.Trigger.AUTOMATIC, last_heartbeat=timezone.now()
        )
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(page.context["latest_job"], running)
        self.assertContains(page, 'data-job-control="gather-status" disabled')
        self.assertContains(page, "Lancée automatiquement")

    def test_a_finished_automatic_run_is_not_the_cards(self):
        manual = gather_job(ScrapeJob.Status.SUCCESS, progress={})
        gather_job(ScrapeJob.Status.SUCCESS, trigger=ScrapeJob.Trigger.AUTOMATIC, progress={})
        self.assertEqual(self.card()["latest_job"], manual)


class ManualOnlyOnConsignesTests(TestCase):
    def page(self):
        return self.client.get(reverse("returnables:home"))

    def slips_progress(self):
        return {slips_code(): {"label": "Bons", "found": 1, "imported": 1}}

    def test_a_finished_automatic_run_is_not_the_bons_card(self):
        gather_job(
            ScrapeJob.Status.SUCCESS,
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            progress=self.slips_progress(),
            finished_at=timezone.now(),
        )
        self.assertIsNone(self.page().context["gather_job"])

    def test_a_running_automatic_run_is_shown_and_said(self):
        running = gather_job(
            ScrapeJob.Status.RUNNING,
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            progress=self.slips_progress(),
            last_heartbeat=timezone.now(),
        )
        page = self.page()
        self.assertEqual(page.context["gather_job"], running)
        self.assertContains(page, "Une récupération automatique est en cours.")

    def test_a_manual_run_still_shows_as_before(self):
        manual = gather_job(ScrapeJob.Status.SUCCESS, progress=self.slips_progress(), finished_at=timezone.now())
        self.assertEqual(self.page().context["gather_job"], manual)

    def test_the_bons_card_links_to_the_automatic_gathers(self):
        self.assertContains(self.page(), f'href="{reverse("invoices:auto_gathers")}"')


class UnattendedGatherTests(Sources, TestCase):
    """`unattended`: where each source starts (with no history, its own
    start, 90 days back at most - test_auto_gather_review has the rest),
    headless portals, no Metro."""

    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.floor = timezone.localdate() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    def gather(self, codes, start=None, **kwargs):
        job = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC)
        with (
            mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]) as email,
            mock.patch("invoices.tasks.find_matching_emails", return_value=[]) as slips,
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]) as portal,
            mock.patch("invoices.tasks.scrape_metro_invoices", return_value=[]) as metro,
            mock.patch("notifications.events.emit"),
        ):
            gather_invoices_task(job.id, start, None, set(codes), False, **kwargs)
        job.refresh_from_db()
        return job, email, slips, portal, metro

    def test_a_mailbox_source_never_searched_starts_from_its_own_start(self):
        job, email, *_ = self.gather({self.email_code}, unattended=True)
        self.assertEqual(email.call_args.args[1], self.floor)
        self.assertEqual(job.range_start, self.floor)
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_by_hand_the_same_source_searches_its_own_start(self):
        _job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.floor)

    def test_a_posted_start_is_kept(self):
        start = timezone.localdate() - timedelta(days=3)
        _job, email, *_ = self.gather({self.email_code}, start=start, unattended=True)
        self.assertEqual(email.call_args.args[1], start)

    def test_the_slips_never_searched_start_from_their_own_start(self):
        _job, _email, slips, *_ = self.gather({self.slips_code}, unattended=True)
        self.assertEqual(slips.call_args.args[0], self.floor)

    def test_a_portal_runs_headless_whatever_its_window_setting(self):
        WebsiteInvoiceSource.objects.filter(invoice_type=self.box).update(show_browser=True)
        _job, *_rest, portal, _metro = self.gather({self.portal_code}, unattended=True)
        self.assertTrue(portal.call_args.kwargs["headless"])
        self.assertFalse(portal.call_args.args[0].show_browser)
        self.assertEqual(portal.call_args.args[2], self.floor)

    def test_by_hand_a_portal_keeps_its_window(self):
        WebsiteInvoiceSource.objects.filter(invoice_type=self.box).update(show_browser=True)
        _job, *_rest, portal, _metro = self.gather({self.portal_code})
        self.assertTrue(portal.call_args.args[0].show_browser)

    def test_metro_is_never_signed_in_to(self):
        job, *_rest, metro = self.gather({"METRO", self.email_code}, unattended=True)
        metro.assert_not_called()
        self.assertIn("Metro : à la main seulement", job.log)
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)


class EndOfGatherAlertTests(Sources, TestCase):
    """tasks._notify_auto_gather, once the job's final status is saved."""

    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.rule = AutoGather.objects.create(
            name="Factures exemple",
            sources=[self.email_code],
            weekdays="2",
            start_time=time(7, 0),
            end_time=time(7, 0),
            every_minutes=60,
            last_failed_codes=["type-999"],
        )

    def gather(self, codes=None, *, trigger=ScrapeJob.Trigger.AUTOMATIC, files=(), email_error=None, **patches):
        job = ScrapeJob.objects.create(trigger=trigger, auto_gather_id=self.rule.pk)
        email = mock.patch(
            "invoices.tasks.scrape_email_invoices",
            side_effect=email_error,
            return_value=list(files),
        )
        with (
            email,
            mock.patch("invoices.tasks.find_matching_emails", return_value=[]),
            mock.patch("invoices.receipts.import_document"),
            mock.patch("notifications.events.emit", **patches) as emit,
        ):
            gather_invoices_task(job.id, None, None, codes or {self.email_code}, False, unattended=True)
        job.refresh_from_db()
        self.rule.refresh_from_db()
        return job, emit

    def test_a_failing_source_is_said_once_a_day(self):
        job, emit = self.gather(email_error=OSError("connexion refusée"))
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertEqual(self.rule.last_failed_codes, [self.email_code])
        args, kwargs = emit.call_args
        self.assertEqual(args, ("invoices-auto-gather", "failed"))
        day = timezone.localdate(job.finished_at).isoformat()
        self.assertEqual(kwargs["content_key"], f"gather-failed:{self.rule.pk}:{self.email_code}:{day}")
        self.assertEqual(kwargs["body"], "Échec : Cave Exemple - Factures.")
        self.assertEqual(kwargs["title"], "Récupération automatique : une source a échoué")
        self.assertEqual(kwargs["target"], f"{reverse('invoices:invoice_list')}?ajouter=recuperer")

    def test_new_invoices(self):
        job, emit = self.gather(files=[("/tmp/facture-1.pdf", date(2026, 10, 1))])
        self.assertEqual(job.invoices_created, 1)
        args, kwargs = emit.call_args
        self.assertEqual(args, ("invoices-auto-gather", "new"))
        self.assertEqual(kwargs["content_key"], f"gather:{job.pk}")
        self.assertEqual(kwargs["body"], "1 nouvelle facture")
        self.assertEqual(self.rule.last_failed_codes, [])

    def test_nothing_new(self):
        job, emit = self.gather()
        args, kwargs = emit.call_args
        self.assertEqual(args, ("invoices-auto-gather", "nothing"))
        self.assertEqual(kwargs["content_key"], f"gather:{job.pk}")
        self.assertEqual(kwargs["body"], "Aucune nouvelle facture")

    def test_a_run_failed_as_a_whole(self):
        with mock.patch("invoices.tasks._mailed_slip_formats", side_effect=RuntimeError("base verrouillée")):
            job, emit = self.gather()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        self.assertEqual(self.rule.last_failed_codes, ["*"])
        self.assertEqual(emit.call_args.args[1], "failed")
        self.assertIn(f":{self.rule.pk}:*:", emit.call_args.kwargs["content_key"])
        self.assertEqual(emit.call_args.kwargs["body"], "Échec : la récupération entière.")

    def test_slips_alone_open_consignes(self):
        _job, emit = self.gather({self.slips_code})
        self.assertEqual(emit.call_args.kwargs["target"], reverse("returnables:home"))
        self.assertEqual(emit.call_args.kwargs["body"], "Aucune nouvelle facture · aucun nouveau bon")

    def test_a_cancelled_run_says_nothing(self):
        with mock.patch("invoices.tasks._is_cancelled", return_value=True):
            job, emit = self.gather()
        self.assertEqual(job.status, ScrapeJob.Status.CANCELLED)
        emit.assert_not_called()
        self.assertEqual(self.rule.last_failed_codes, ["type-999"])

    def test_a_refused_run_says_nothing(self):
        with mock.patch("invoices.tasks.integrations_allowed", return_value=False):
            job, emit = self.gather()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        emit.assert_not_called()

    def test_a_run_by_hand_says_nothing(self):
        _job, emit = self.gather(trigger=ScrapeJob.Trigger.MANUAL)
        emit.assert_not_called()
        self.assertEqual(self.rule.last_failed_codes, ["type-999"])

    def test_an_alert_that_cannot_be_made_never_touches_the_gather(self):
        with self.assertLogs("invoices.tasks", "ERROR"):
            job, _emit = self.gather(side_effect=RuntimeError("table absente"))
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertIsNotNone(job.finished_at)
