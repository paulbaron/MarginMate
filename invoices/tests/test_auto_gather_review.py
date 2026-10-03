"""The automatic gathers after their review (findings 1, 11-15, 19, 20): a
failure after a slot is claimed, two rules due together, a schedule nobody
can read, the catch-up limit each rule states, where each source of an
unattended run starts, the alert of a run that stored bons, and the start
of a portal gathered by hand.

No gather ever runs for real: `invoices.gathering.threading.Thread` is
patched wherever a slot could start one, and the task's mailbox, portal and
Metro calls are replaced. Every name and date is invented.
"""

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, OperationalError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from invoices import auto_gather, gathering
from invoices.models import AutoGather, ScrapeJob
from invoices.tasks import DEFAULT_LOOKBACK_DAYS, OVERLAP_DAYS, default_gather_start, gather_invoices_task
from invoices.tests.test_auto_gather import WEDNESDAY, WEDNESDAY_6AM, RunDueCase, Sources
from invoices.tests.test_auto_gather_views import PAGE, PageCase, make_rule
from tests.factories import make_invoice


def at(hours=0, minutes=0):
    """Wednesday 07/10/2026 in Paris, `hours`:`minutes` after 06:00."""
    return WEDNESDAY_6AM + timedelta(hours=hours, minutes=minutes)


class FailureAfterTheClaimTests(RunDueCase):
    """[1] Whatever fails once a slot is claimed is said on the rule, and
    neither delays the next rule nor the prune."""

    def test_a_launch_that_raises_is_said_and_the_next_rule_and_the_prune_still_run(self):
        failing = self.rule()
        other = self.rule(name="Factures exemple", sources=[self.email_code], every_minutes=60)
        old = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC, status=ScrapeJob.Status.SUCCESS)
        ScrapeJob.objects.filter(pk=old.pk).update(started_at=WEDNESDAY_6AM - timedelta(days=40))
        real = gathering.start_gather

        def start_gather(codes, *args, auto_gather_id=None, **kwargs):
            if auto_gather_id == failing.pk:
                raise RuntimeError("bogue")
            return real(codes, *args, auto_gather_id=auto_gather_id, **kwargs)

        with (
            mock.patch("invoices.auto_gather.gathering.start_gather", side_effect=start_gather),
            self.assertLogs("invoices.auto_gather", "ERROR"),
        ):
            thread = self.run_due(at(minutes=1))
        failing.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(failing.last_result, "échec : erreur interne à 06:01")
        self.assertEqual(failing.last_slot_at, at())
        self.assertTrue(other.last_result.startswith("lancée à 06:01"))
        thread.return_value.start.assert_called_once()
        self.assertFalse(ScrapeJob.objects.filter(pk=old.pk).exists())

    def test_a_database_locked_at_the_launch_gives_the_slot_back(self):
        """[R7] The scheduler waits 5 s for a write lock: a locked
        start_gather created nothing, so the slot is tried again."""
        rule = self.rule()
        with (
            mock.patch(
                "invoices.auto_gather.gathering.start_gather", side_effect=OperationalError("database is locked")
            ),
            self.assertLogs("invoices.auto_gather", "WARNING"),
        ):
            self.run_due(at(minutes=1))
        rule.refresh_from_db()
        self.assertEqual(rule.last_result, auto_gather.BUSY)
        self.assertEqual(rule.last_result, "en attente : base occupée")
        self.assertIsNone(rule.last_slot_at)
        self.assertFalse(ScrapeJob.objects.exists())

        thread = self.run_due(at(minutes=2))
        rule.refresh_from_db()
        job = ScrapeJob.objects.get(auto_gather_id=rule.pk)
        self.assertEqual(rule.last_result, f"lancée à 06:02 (récupération n° {job.pk})")
        self.assertEqual(rule.last_slot_at, at())
        thread.return_value.start.assert_called_once()

    def test_a_database_locked_past_the_catch_up_limit_is_said_as_such(self):
        rule = self.rule(
            name="Factures", sources=[self.email_code], start_time=time(7), end_time=time(7), every_minutes=60
        )
        with (
            mock.patch(
                "invoices.auto_gather.gathering.start_gather", side_effect=OperationalError("database is locked")
            ),
            self.assertLogs("invoices.auto_gather", "WARNING"),
        ):
            self.run_due(at(hours=1, minutes=1))
        thread = self.run_due(at(hours=13, minutes=1))
        rule.refresh_from_db()
        self.assertEqual(rule.last_result, "manquée : base occupée à 07:00")
        self.assertEqual(rule.last_slot_at, at(hours=1))
        thread.assert_not_called()

    def test_a_result_that_cannot_be_written_is_logged_never_raised(self):
        self.rule()
        with (
            mock.patch("invoices.auto_gather._say", side_effect=DatabaseError("database is locked")),
            self.assertLogs("invoices.auto_gather", "WARNING") as logs,
        ):
            self.run_due(at(minutes=1))
        self.assertIn("résultat non enregistré", "\n".join(logs.output))

    def test_one_rule_raising_anywhere_leaves_the_others_alone(self):
        self.rule()
        other = self.rule(name="Factures exemple", sources=[self.email_code], every_minutes=60)
        real = auto_gather.run_rule

        def run_rule(rule, now, *, enabled):
            if rule.pk != other.pk:
                raise DatabaseError("database is locked")
            return real(rule, now, enabled=enabled)

        with (
            mock.patch("invoices.auto_gather.run_rule", side_effect=run_rule),
            self.assertLogs("invoices.auto_gather", "ERROR"),
        ):
            self.run_due(at(minutes=1))
        other.refresh_from_db()
        self.assertTrue(other.last_result.startswith("lancée"))


class TwoRulesDueTogetherTests(RunDueCase):
    """[11] The page's own suggested pair: slips every 30 min from 06:00 to
    14:00, and invoices once a day at 07:00. The second waits for the first
    instead of losing its slot."""

    def pair(self):
        slips = self.rule(weekdays="0,1,2,3,4,5")
        invoices = self.rule(
            name="Factures du matin",
            sources=[self.email_code],
            weekdays="0,1,2,3,4,5",
            start_time=time(7, 0),
            end_time=time(7, 0),
            every_minutes=60,
        )
        AutoGather.objects.filter(pk=slips.pk).update(last_slot_at=at(minutes=30))
        return slips, invoices

    def test_the_second_starts_once_the_first_has_finished(self):
        slips, invoices = self.pair()
        self.run_due(at(hours=1, minutes=1))
        invoices.refresh_from_db()
        self.assertEqual(invoices.last_result, auto_gather.WAITING)
        self.assertIsNone(invoices.last_slot_at)
        self.assertEqual(ScrapeJob.objects.count(), 1)

        ScrapeJob.objects.update(status=ScrapeJob.Status.SUCCESS)
        thread = self.run_due(at(hours=1, minutes=3))
        invoices.refresh_from_db()
        job = ScrapeJob.objects.get(auto_gather_id=invoices.pk)
        self.assertEqual(invoices.last_result, f"lancée à 07:03 (récupération n° {job.pk})")
        self.assertEqual(invoices.last_slot_at, at(hours=1))
        thread.return_value.start.assert_called_once()
        self.assertEqual(ScrapeJob.objects.filter(auto_gather_id=slips.pk).count(), 1)

    def test_a_gather_running_past_the_catch_up_limit_is_said_as_such(self):
        _slips, invoices = self.pair()
        self.run_due(at(hours=1, minutes=1))
        # The slips' gather is still running twelve hours later.
        self.run_due(at(hours=13, minutes=1))
        invoices.refresh_from_db()
        self.assertEqual(invoices.last_result, "manquée : une récupération était en cours à 07:00")
        self.assertEqual(invoices.last_slot_at, at(hours=1))


class UnreadableScheduleTests(RunDueCase):
    """[12] end before start, or days nobody can read."""

    def bad_rule(self, **fields) -> AutoGather:
        values = {
            "name": "Mal réglée",
            "sources": [self.slips_code],
            "weekdays": "0,1,2,3,4,5",
            "start_time": time(14, 0),
            "end_time": time(6, 0),
            "every_minutes": 30,
            "created_at": WEDNESDAY_6AM - timedelta(days=7),
            **fields,
        }
        rule = AutoGather(**values)
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA ignore_check_constraints = ON")
        try:
            rule.save()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = OFF")
        return rule

    def test_the_model_refuses_an_end_before_the_start(self):
        rule = AutoGather(
            name="Mal réglée",
            sources=[self.slips_code],
            weekdays="2",
            start_time=time(14, 0),
            end_time=time(6, 0),
            every_minutes=30,
        )
        with self.assertRaises(ValidationError) as refused:
            rule.full_clean()
        self.assertEqual(refused.exception.message_dict["end_time"], ["L'heure de fin vient avant celle du début."])

    def test_the_database_refuses_it_too(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AutoGather.objects.create(
                name="Mal réglée", weekdays="2", start_time=time(14, 0), end_time=time(6, 0), every_minutes=30
            )

    def test_once_a_day_is_still_allowed(self):
        AutoGather(
            name="Factures", sources=[self.email_code], weekdays="2", start_time=time(7), end_time=time(7)
        ).full_clean()

    def test_an_unreadable_rule_is_claimed_once_and_said_without_a_warning_a_minute(self):
        broken = self.rule(weekdays="9")
        with self.assertLogs("invoices.auto_gather", "WARNING") as logs:
            self.run_due(at(minutes=1))
        self.assertEqual(len(logs.output), 1)
        broken.refresh_from_db()
        self.assertEqual(broken.last_result, "sautée : jours ou heures illisibles")
        self.assertEqual(broken.last_slot_at, at(minutes=1))
        with self.assertNoLogs("invoices.auto_gather", "WARNING"):
            self.run_due(at(minutes=2))
        broken.refresh_from_db()
        self.assertEqual(broken.last_slot_at, at(minutes=1))

    def test_hours_stored_upside_down_are_said_the_same_way(self):
        broken = self.bad_rule()
        self.run_due(at(minutes=1))
        broken.refresh_from_db()
        self.assertEqual(broken.last_result, auto_gather.UNREADABLE)


class UnreadableCardTests(PageCase):
    def test_the_page_survives_a_stored_bad_row(self):
        make_rule(name="Bonne")
        bad = AutoGather(
            name="Mal réglée",
            sources=[self.slips_code],
            weekdays="0,1,2,3,4,5",
            start_time=time(14, 0),
            end_time=time(6, 0),
            every_minutes=30,
        )
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA ignore_check_constraints = ON")
        try:
            bad.save()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = OFF")
        text = self.text(self.html())
        self.assertIn("heures illisibles : corrigez-les", text)
        self.assertIn("Prochaines récupérations", text)


class CatchUpLimitTests(RunDueCase):
    """[13] min(every_minutes, 120) for a rule that runs several times a day,
    twelve hours for a once-a-day one - and the page says which."""

    def once_a_day(self):
        return self.rule(
            name="Factures", sources=[self.email_code], start_time=time(7), end_time=time(7), every_minutes=60
        )

    def test_a_once_a_day_rule_is_caught_up_after_its_hour(self):
        rule = self.once_a_day()
        thread = self.run_due(at(hours=2, minutes=10))
        thread.return_value.start.assert_called_once()
        rule.refresh_from_db()
        self.assertTrue(rule.last_result.startswith("lancée à 08:10"))

    def test_a_once_a_day_rule_is_missed_after_twelve_hours(self):
        rule = self.once_a_day()
        thread = self.run_due(at(hours=13))
        thread.assert_not_called()
        rule.refresh_from_db()
        self.assertEqual(rule.last_result, "manquée : serveur arrêté à 07:00")

    def test_the_limits(self):
        self.assertEqual(auto_gather.catch_up_limit(self.once_a_day()), timedelta(hours=12))
        self.assertEqual(auto_gather.catch_up_limit(self.rule(every_minutes=30)), timedelta(minutes=30))
        self.assertEqual(auto_gather.catch_up_limit(self.rule(every_minutes=360)), timedelta(hours=2))


class CatchUpOnThePageTests(PageCase):
    def test_each_card_states_its_own_limit_and_the_explainer_the_rule(self):
        make_rule(every_minutes=30)
        make_rule(name="Factures", sources=[self.email_code], start_time=time(7), end_time=time(7), every_minutes=60)
        text = self.text(self.html())
        self.assertIn("Une heure manquée est rattrapée dans les 30 min.", text)
        self.assertIn("Une heure manquée est rattrapée dans les 12 h.", text)
        self.assertNotIn("de plus de deux heures", text)
        self.assertIn("12 h pour une fois par jour", text)


def finished_gather(code, days_ago, *, error=False, status=ScrapeJob.Status.SUCCESS, **fields):
    """A gather finished `days_ago` days ago (at noon in Paris) whose progress
    holds `code`, in error or not."""
    entry = {"label": "Source", "found": 0, "imported": 0}
    if error:
        entry["error"] = "Boîte mail : refusée"
    job = ScrapeJob.objects.create(status=status, progress={code: entry}, **fields)
    noon = timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days_ago), time(12)))
    ScrapeJob.objects.filter(pk=job.pk).update(started_at=noon)
    return job


class UnattendedStartTests(Sources, TestCase):
    """[14] + [20] Each source of an automatic run starts where its coverage
    stops (invoices/coverage.py, read once from the history when unknown),
    90 days back at most - and says, until a search covers it, a stretch that
    bound leaves never searched."""

    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.today = timezone.localdate()
        self.rule = AutoGather.objects.create(
            name="Factures exemple",
            sources=[self.email_code],
            weekdays=str(WEDNESDAY),
            start_time=time(7, 0),
            end_time=time(7, 0),
        )

    def days_ago(self, days) -> date:
        return self.today - timedelta(days=days)

    def gather(self, codes, *, unattended=True, start=None):
        job = ScrapeJob.objects.create(
            trigger=ScrapeJob.Trigger.AUTOMATIC if unattended else ScrapeJob.Trigger.MANUAL,
            auto_gather_id=self.rule.pk,
        )
        with (
            mock.patch("invoices.tasks.scrape_email_invoices", return_value=[]) as email,
            mock.patch("invoices.tasks.find_matching_emails", return_value=[]) as slips,
            mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]) as portal,
            mock.patch("notifications.events.emit") as emit,
        ):
            gather_invoices_task(job.id, start, None, set(codes), False, unattended=unattended)
        job.refresh_from_db()
        return job, email, slips, portal, emit

    def test_a_monthly_biller_searched_yesterday_starts_yesterday_less_the_overlap(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(30))
        finished_gather(self.email_code, 1)
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(1 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_a_three_week_outage_is_searched_whole(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(30))
        finished_gather(self.email_code, 22)
        for days in range(21, 0, -1):
            finished_gather(self.email_code, days, error=True, trigger=ScrapeJob.Trigger.AUTOMATIC)
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(22 + OVERLAP_DAYS))
        self.assertEqual(job.range_start, self.days_ago(22 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_a_failed_or_cancelled_run_is_no_clean_search(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(30))
        finished_gather(self.email_code, 2, status=ScrapeJob.Status.CANCELLED)
        finished_gather(self.email_code, 1, error=True)
        _job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(30 + OVERLAP_DAYS))

    def test_never_searched_starts_from_its_own_start(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(40 + OVERLAP_DAYS))
        self.assertNotIn("catch_up", job.progress[self.email_code])

    def test_a_misdated_invoice_in_the_future_never_moves_it_past_today(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(40))
        make_invoice(supplier=self.cave.supplier, invoice_date=self.today + timedelta(days=300))
        _job, email, *_ = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.days_ago(40 + OVERLAP_DAYS))

    def test_a_stretch_past_ninety_days_is_flagged_until_a_search_covers_it(self):
        make_invoice(supplier=self.cave.supplier, invoice_date=self.days_ago(200))
        job, email, _slips, _portal, emit = self.gather({self.email_code})
        bound = self.days_ago(DEFAULT_LOOKBACK_DAYS)
        self.assertEqual(email.call_args.args[1], bound)
        sentence = (
            "Rattrapage à faire à la main depuis Factures, "
            f"du {self.days_ago(200 + OVERLAP_DAYS):%d/%m/%Y} au {bound:%d/%m/%Y}"
        )
        self.assertEqual(job.progress[self.email_code]["catch_up"], sentence)
        self.assertNotIn("error", job.progress[self.email_code])
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))
        self.assertIn(sentence, emit.call_args.kwargs["body"])
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.last_failed_codes, [self.email_code])

        # [R4] Said again by every run until a search covers it
        # (test_auto_gather_coverage.PendingCatchUpTests).
        again, email, _slips, _portal, emit = self.gather({self.email_code})
        self.assertEqual(email.call_args.args[1], self.today - timedelta(days=OVERLAP_DAYS))
        self.assertEqual(again.progress[self.email_code]["catch_up"], sentence)
        self.assertEqual(emit.call_args.args, ("invoices-auto-gather", "failed"))

    def test_slips_start_where_their_last_clean_search_left_off(self):
        finished_gather(self.slips_code, 1)
        _job, _email, slips, *_ = self.gather({self.slips_code})
        self.assertEqual(slips.call_args.args[0], self.days_ago(1 + OVERLAP_DAYS))

    def test_by_hand_nothing_changes(self):
        finished_gather(self.email_code, 1)
        _job, email, *_ = self.gather({self.email_code}, unattended=False)
        self.assertEqual(email.call_args.args[1], self.days_ago(DEFAULT_LOOKBACK_DAYS))


class UnattendedStartOnThePageTests(PageCase):
    def test_the_page_says_where_each_source_starts(self):
        text = self.text(self.html())
        self.assertIn(
            "Chaque source reprend là où sa dernière récupération réussie s'est arrêtée, 90 jours au plus.", text
        )
        self.assertNotIn("14 jours", text)
        self.assertNotIn("premier rattrapage", text)

    def test_a_run_with_a_catch_up_to_do_says_it(self):
        rule = make_rule()
        ScrapeJob.objects.create(
            trigger=ScrapeJob.Trigger.AUTOMATIC,
            auto_gather_id=rule.pk,
            status=ScrapeJob.Status.SUCCESS,
            progress={
                self.email_code: {
                    "label": "Cave Exemple - Factures",
                    "found": 0,
                    "imported": 0,
                    "catch_up": "Rattrapage à faire à la main depuis Factures, du 01/01/2026 au 04/07/2026",
                }
            },
        )
        text = self.text(self.html(PAGE))
        self.assertIn(
            "Cave Exemple - Factures : Rattrapage à faire à la main depuis Factures, du 01/01/2026 au 04/07/2026", text
        )


class SlipsAloneAlertTests(Sources, TestCase):
    """[15] An automatic run that stored new bons is « du nouveau »."""

    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)

    def test_new_bons_alone_are_news(self):
        job = ScrapeJob.objects.create(trigger=ScrapeJob.Trigger.AUTOMATIC)
        with (
            mock.patch("invoices.tasks.find_matching_emails", return_value=[]),
            mock.patch("returnables.mail.store_matches", return_value=(2, 2, None)),
            mock.patch("notifications.events.emit") as emit,
        ):
            gather_invoices_task(job.id, None, None, {self.slips_code}, False, unattended=True)
        args, kwargs = emit.call_args
        self.assertEqual(args, ("invoices-auto-gather", "new"))
        self.assertEqual(kwargs["title"], "Récupération automatique : du nouveau")
        self.assertEqual(kwargs["body"], "Aucune nouvelle facture · 2 bons de consignes")


class PortalStartByHandTests(Sources, TestCase):
    """[19] Automatic mailbox imports move Achats' offered start; a portal
    gathered by hand from it still reaches back to its own newest invoice."""

    def setUp(self):
        self.make_sources()
        heartbeat = mock.patch("invoices.tasks._GatherHeartbeat")
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.today = timezone.localdate()

    def gather(self, start):
        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks.fetch_website_invoices", return_value=[]) as portal:
            gather_invoices_task(job.id, start, None, {self.portal_code}, False)
        return portal

    def test_the_offered_start_is_lowered_to_the_portals_own(self):
        # Read by its supplier's own reader (ocr_text blank): moves Achats' start.
        make_invoice(supplier=self.cave.supplier, invoice_date=self.today - timedelta(days=1))
        # Read as a ticket reads a portal's PDF (ocr_text kept).
        make_invoice(supplier=self.box.supplier, invoice_date=self.today - timedelta(days=20), ocr_text="Texte lu")
        offered = default_gather_start({self.cave.supplier_id, self.box.supplier_id})
        self.assertEqual(offered, self.today - timedelta(days=1))
        portal = self.gather(offered)
        self.assertEqual(portal.call_args.args[2], self.today - timedelta(days=20 + OVERLAP_DAYS))

    def test_an_earlier_posted_start_is_kept(self):
        make_invoice(supplier=self.box.supplier, invoice_date=self.today - timedelta(days=20), ocr_text="Texte lu")
        start = self.today - timedelta(days=60)
        self.assertEqual(self.gather(start).call_args.args[2], start)

    def test_a_future_invoice_never_moves_it_forward(self):
        make_invoice(supplier=self.box.supplier, invoice_date=self.today - timedelta(days=20), ocr_text="Texte lu")
        make_invoice(supplier=self.box.supplier, invoice_date=self.today + timedelta(days=300), ocr_text="Texte lu")
        portal = self.gather(self.today - timedelta(days=1))
        self.assertEqual(portal.call_args.args[2], self.today - timedelta(days=20 + OVERLAP_DAYS))

    def test_a_failing_portal_never_widens_the_period_achats_offers_again(self):
        """[R0] Searched from its own start, the period stays the one posted:
        Achats offers it again to every source after a failure."""
        from django.urls import reverse

        from invoices.scrapers.website import WebsiteError

        make_invoice(supplier=self.cave.supplier, invoice_date=self.today - timedelta(days=1))
        offered = self.today - timedelta(days=1)
        job = ScrapeJob.objects.create()
        with mock.patch(
            "invoices.tasks.fetch_website_invoices", side_effect=WebsiteError("code SMS demandé")
        ) as portal:
            gather_invoices_task(job.id, offered, None, {self.portal_code}, False)
        job.refresh_from_db()
        self.assertEqual(portal.call_args.args[2], self.today - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        self.assertEqual(job.range_start, offered)
        self.assertIn(f"recherche depuis le {self.today - timedelta(days=DEFAULT_LOOKBACK_DAYS):%d/%m/%Y}", job.log)
        card = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer").context
        self.assertEqual(card["default_start_date"], offered)

    def test_no_invoice_reaches_back_the_default_lookback(self):
        portal = self.gather(self.today - timedelta(days=1))
        self.assertEqual(portal.call_args.args[2], self.today - timedelta(days=DEFAULT_LOOKBACK_DAYS))
