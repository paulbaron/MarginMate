"""The period an automatic sales import fetches (recipes/auto_sales.py):
END the last complete till day - by the espace's « La nuit se termine à »,
or the calendar -, START from the till's coverage (`ventes-laddition`, the
gathers' GatherCoverage and rules) less 3 days, from its own start, from the
history once, never before today − 400 days; and the coverage an import
records when it completes - by hand from the Ventes tab too -, never a
failed or cancelled one.

Nothing is downloaded: the task runs with `download_sales_lines` and the
file's reading patched. Data invented.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from invoices.models import GatherCoverage
from notifications.models import NotificationSettings
from recipes import auto_sales
from recipes.models import AutoSalesImport, PosProduct, PosProductDailyQuantity, SalesImportJob
from recipes.pos.laddition_download import DownloadCancelled
from recipes.pos.laddition_xlsx import ParsedExport
from recipes.tasks import import_laddition_sales_task
from tests.factories import make_recipe

WEDNESDAY = date(2026, 11, 18)
CODE = "ventes-laddition"


def paris(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall time in Paris in winter (UTC+1)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(hours=1)


def night(at: time) -> None:
    NotificationSettings.objects.update_or_create(pk=NotificationSettings.SINGLETON_PK, defaults={"night_ends_at": at})


def cover(until) -> None:
    GatherCoverage.objects.update_or_create(code=CODE, defaults={"searched_until": until})


def covered() -> date | None:
    return GatherCoverage.objects.get(code=CODE).searched_until


class EndTests(TestCase):
    def test_before_the_night_ends_yesterday_is_still_open(self):
        night(time(6, 0))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 5, 59)), date(2026, 11, 16))

    def test_at_the_night_s_end_yesterday_is_complete(self):
        night(time(6, 0))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 6, 0)), date(2026, 11, 17))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 23, 0)), date(2026, 11, 17))

    def test_the_default_night_is_six(self):
        self.assertFalse(NotificationSettings.objects.exists())
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 5, 0)), date(2026, 11, 16))
        self.assertFalse(NotificationSettings.objects.exists(), "a read writes no settings row")

    def test_the_calendar_is_yesterday_at_any_hour(self):
        night(time(0, 0))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 0, 30)), date(2026, 11, 17))

    def test_a_later_night(self):
        night(time(11, 0))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 10, 0)), date(2026, 11, 16))
        self.assertEqual(auto_sales.last_complete_day(paris(WEDNESDAY, 11, 0)), date(2026, 11, 17))


class StartTests(TestCase):
    NOW = paris(WEDNESDAY, 7, 0)

    def period(self):
        return auto_sales.period_for("laddition", self.NOW)

    def test_from_the_coverage_less_three_days(self):
        cover(date(2026, 11, 10))
        period = self.period()
        self.assertEqual((period.start, period.end, period.cut), (date(2026, 11, 7), date(2026, 11, 17), False))

    def test_covered_up_to_the_last_complete_day_is_up_to_date(self):
        cover(date(2026, 11, 17))
        period = self.period()
        self.assertTrue(period.up_to_date)
        self.assertEqual(period.covered_until, date(2026, 11, 17))

    def test_never_covered_from_the_newest_sales_day_less_three(self):
        product = PosProduct.objects.create(name="Pinte Exemple")
        for day in (date(2026, 11, 2), date(2026, 11, 14)):
            PosProductDailyQuantity.objects.create(product=product, sold_on=day, quantity=4)
        self.assertEqual(self.period().start, date(2026, 11, 11))
        self.assertIsNone(covered(), "an unknown coverage is made once, from no history")

    def test_never_covered_and_no_sales_ninety_days_back(self):
        self.assertEqual(self.period().start, WEDNESDAY - timedelta(days=90))

    def test_unknown_is_read_once_from_the_successful_imports(self):
        reached = SalesImportJob.objects.create(
            status=SalesImportJob.Status.SUCCESS,
            range_start=date(2026, 11, 1),
            range_end=date(2026, 11, 12),
            finished_at=paris(date(2026, 11, 15), 12),
        )
        # A failed import, however far it was asked to go, says nothing.
        SalesImportJob.objects.create(
            status=SalesImportJob.Status.FAILED, range_start=date(2026, 11, 1), range_end=date(2026, 11, 16)
        )
        # An import up to the day it ran reached the last complete day only.
        SalesImportJob.objects.create(
            status=SalesImportJob.Status.SUCCESS,
            range_start=date(2026, 10, 1),
            range_end=date(2026, 10, 20),
            finished_at=paris(date(2026, 10, 20), 15),
        )
        self.assertEqual(self.period().start, date(2026, 11, 9))
        self.assertEqual(covered(), date(2026, 11, 12))
        self.assertTrue(reached.pk)

    def test_never_more_than_four_hundred_days_back(self):
        cover(date(2025, 1, 1))
        period = self.period()
        self.assertEqual(period.start, WEDNESDAY - timedelta(days=400))
        self.assertEqual(period.start, date(2025, 10, 14))
        self.assertTrue(period.cut)

    def test_the_cut_is_said_in_the_job_s_log(self):
        cover(date(2025, 1, 1))
        rule = AutoSalesImport.objects.create(
            name="Ventes exemple", weekdays="0,1,2,3,4,5,6", times="07:00", created_at=self.NOW - timedelta(days=1)
        )
        with (
            override_settings(BASE_DIR=Path(tempfile.mkdtemp())),
            mock.patch("notifications.webpush.sending_enabled", return_value=True),
            mock.patch("recipes.importing.threading.Thread"),
        ):
            auto_sales.run_due(self.NOW + timedelta(minutes=1))
        job = SalesImportJob.objects.get(auto_rule_id=rule.pk)
        self.assertEqual(job.range_start, date(2025, 10, 14))
        self.assertIn("Début ramené au 14/10/2025 (400 jours au plus)", job.log)


class RecordedTests(TestCase):
    """What an import records, run as the thread runs it - dates around the
    real today, since the task reads the clock."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        make_recipe(name="Pinte Exemple", selling_price_ttc="6.50")

    def day(self, back: int) -> date:
        return self.today - timedelta(days=back)

    def run_task(self, start, end, *, download=None, trigger=SalesImportJob.Trigger.MANUAL):
        sold_on = start
        export = ParsedExport(
            products={
                "Pinte Exemple": {"quantity": 3, "category": "", "typology": "", "first": sold_on, "last": sold_on}
            },
            entries=[("Pinte Exemple", sold_on, 3)],
        )
        job = SalesImportJob.objects.create(range_start=start, range_end=end, trigger=trigger)
        with (
            mock.patch("recipes.tasks.download_sales_lines", **(download or {"return_value": ["ventes.xlsx"]})),
            mock.patch("recipes.tasks.parse_sales_exports", return_value=export),
            mock.patch("notifications.events.emit"),
        ):
            import_laddition_sales_task(job.pk, start, end, download_dir="non-utilisé")
        job.refresh_from_db()
        return job

    def test_a_manual_import_continuing_the_coverage_moves_it(self):
        cover(self.day(11))
        job = self.run_task(self.day(10), self.day(2))
        self.assertEqual(job.status, SalesImportJob.Status.SUCCESS)
        self.assertEqual(covered(), self.day(2))

    def test_an_import_up_to_today_moves_it_to_the_last_complete_day_only(self):
        cover(self.day(11))
        self.run_task(self.day(10), self.today)
        self.assertEqual(covered(), auto_sales.last_complete_day(timezone.now()))

    def test_an_import_after_a_gap_leaves_it_where_it_was(self):
        cover(self.day(20))
        self.run_task(self.day(10), self.day(2))
        self.assertEqual(covered(), self.day(20))

    def test_a_failed_import_records_nothing(self):
        cover(self.day(11))
        job = self.run_task(self.day(10), self.day(2), download={"side_effect": RuntimeError("Connexion refusée.")})
        self.assertEqual(job.status, SalesImportJob.Status.FAILED)
        self.assertEqual(covered(), self.day(11))

    def test_a_cancelled_import_records_nothing(self):
        cover(self.day(11))
        job = self.run_task(self.day(10), self.day(2), download={"side_effect": DownloadCancelled()})
        self.assertEqual(job.status, SalesImportJob.Status.CANCELLED)
        self.assertEqual(covered(), self.day(11))

    def test_a_first_import_reaching_its_own_start_counts(self):
        """Never covered: the import counts when it starts on or before the
        till's own start (newest sales day less 3) and reaches it."""
        product = PosProduct.objects.create(name="Soda Exemple")
        PosProductDailyQuantity.objects.create(product=product, sold_on=self.day(6), quantity=2)
        self.run_task(self.day(30), self.day(2))
        self.assertEqual(covered(), self.day(2))

    def test_a_first_import_after_its_own_start_pins_it_there(self):
        product = PosProduct.objects.create(name="Soda Exemple")
        PosProductDailyQuantity.objects.create(product=product, sold_on=self.day(20), quantity=2)
        self.run_task(self.day(10), self.day(2))
        # Its own start is 23 days back: the next automatic import starts there.
        self.assertEqual(covered(), self.day(20))

    def test_the_history_never_counts_the_import_being_recorded(self):
        self.run_task(self.day(10), self.day(2))
        self.assertEqual(covered(), self.day(87))
