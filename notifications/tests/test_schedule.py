"""When reminders and automatic gathers are due (notifications/schedule.py):
the typed days and times, the bar's night, daylight saving and the previews.
Every date is invented and in the future; every comparison is in UTC."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django.test import SimpleTestCase

from notifications import schedule
from notifications.schedule import PARIS

MIDNIGHT, TWO, SIX = time(0, 0), time(2, 0), time(6, 0)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def paris(*args, fold=0) -> datetime:
    """A wall time in Paris, as an aware UTC instant."""
    return datetime(*args, tzinfo=PARIS, fold=fold).astimezone(UTC)


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def local(instants) -> list[str]:
    """Instants as Paris wall times, for assertions a person can read (the
    day's name spelt here: %a follows the process's locale)."""
    return [f"{DAYS[(wall := dt.astimezone(PARIS)).weekday()]} {wall:%d/%m %H:%M}" for dt in instants]


class WeekdaysTests(SimpleTestCase):
    def test_parsed_from_storage_or_from_boxes(self):
        self.assertEqual(schedule.parse_weekdays("0,2,5"), (0, 2, 5))
        self.assertEqual(schedule.parse_weekdays(["5", "0", "2", "0"]), (0, 2, 5))
        self.assertEqual(schedule.parse_weekdays([6, 0]), (0, 6))
        self.assertEqual(schedule.parse_weekdays(""), ())
        self.assertEqual(schedule.parse_weekdays([]), ())

    def test_anything_but_0_to_6_is_refused(self):
        for values in (["7"], ["-1"], ["a"], ["1.0"], "1,,8", ["\N{ARABIC-INDIC DIGIT ONE}"], [" "]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                schedule.parse_weekdays(values)

    def test_stored_sorted_and_unique(self):
        self.assertEqual(schedule.weekdays_value([5, 0, 2, 2]), "0,2,5")
        self.assertEqual(schedule.weekdays_value([]), "")

    def test_said_in_short(self):
        self.assertEqual(schedule.format_weekdays((0, 2, 5)), "lun., mer. et sam.")
        self.assertEqual(schedule.format_weekdays((0, 6)), "lun. et dim.")
        self.assertEqual(schedule.format_weekdays((1,)), "mar.")
        self.assertEqual(schedule.format_weekdays(range(7)), "tous les jours")
        self.assertEqual(schedule.format_weekdays(()), "")
        self.assertEqual(schedule.DAY_SHORT, ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."))


class TimesTests(SimpleTestCase):
    def test_every_form_typed(self):
        cases = {
            "0": (MIDNIGHT,),
            "2": (TWO,),
            "0h": (MIDNIGHT,),
            "2h30": (time(2, 30),),
            "02h30": (time(2, 30),),
            "2H30": (time(2, 30),),
            "2:30": (time(2, 30),),
            "00:00": (MIDNIGHT,),
            "23:59": (time(23, 59),),
            "0h et 2h": (MIDNIGHT, TWO),
            "00:00 02:00": (MIDNIGHT, TWO),
            "18:00; 2h30, 0": (MIDNIGHT, time(2, 30), time(18, 0)),
            "  02:00   2h  ": (TWO,),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(schedule.parse_times(text), expected)

    def test_what_is_no_time_is_refused_by_name(self):
        for text, token in {
            "25:00": "25:00",
            "0h et 24h": "24h",
            "2:60": "2:60",
            "2h5": "2h5",
            "2:": "2:",
            "midi": "midi",
            "\N{ARABIC-INDIC DIGIT TWO}": "\N{ARABIC-INDIC DIGIT TWO}",
            "-1": "-1",
            "123": "123",
        }.items():
            with self.subTest(text=text), self.assertRaises(ValueError) as refused:
                schedule.parse_times(text)
            self.assertEqual(str(refused.exception), f"« {token} » n'est pas une heure (00:00 à 23:59).")

    def test_a_long_word_is_cut_in_the_refusal(self):
        with self.assertRaises(ValueError) as refused:
            schedule.parse_times("x" * 500)
        self.assertEqual(str(refused.exception), f"« {'x' * 19}… » n'est pas une heure (00:00 à 23:59).")

    def test_at_least_one_at_most_twelve(self):
        for text in ("", "   ", "et", None):
            with self.subTest(text=text), self.assertRaises(ValueError) as refused:
                schedule.parse_times(text)
            self.assertEqual(str(refused.exception), "Indiquez au moins une heure.")
        twelve = " ".join(f"{hour}h" for hour in range(12))
        self.assertEqual(len(schedule.parse_times(twelve)), 12)
        self.assertEqual(len(schedule.parse_times(twelve + " 0h")), 12)
        with self.assertRaises(ValueError) as refused:
            schedule.parse_times(twelve + " 12h")
        self.assertEqual(str(refused.exception), "12 heures au plus.")

    def test_stored_and_said(self):
        self.assertEqual(schedule.times_value((TWO, MIDNIGHT, TWO)), "00:00 02:00")
        self.assertEqual(schedule.parse_times(schedule.times_value((TWO, MIDNIGHT))), (MIDNIGHT, TWO))
        self.assertEqual(schedule.format_times((TWO, MIDNIGHT)), "00:00 et 02:00")
        self.assertEqual(schedule.format_times((time(4, 0), TWO, MIDNIGHT)), "00:00, 02:00 et 04:00")
        self.assertEqual(schedule.format_times((time(18, 5),)), "18:05")


class NightEndTests(SimpleTestCase):
    def test_the_calendar_or_four_to_noon(self):
        for t in (MIDNIGHT, time(4, 0), SIX, time(9, 30), time(12, 0)):
            with self.subTest(t=t):
                self.assertEqual(schedule.check_night_end(t), "")
        for t in (time(0, 1), TWO, time(3, 59), time(12, 1), time(23, 0)):
            with self.subTest(t=t):
                self.assertEqual(
                    schedule.check_night_end(t),
                    "La nuit doit se terminer après la fermeture du bar (04:00 au plus tôt), "
                    "ou à 00:00 pour suivre le calendrier.",
                )
        self.assertEqual(schedule.DEFAULT_NIGHT_END, SIX)


class NightRuleTests(SimpleTestCase):
    """The week of Monday 02/11/2026 (winter time, UTC+1)."""

    WEEK = (paris(2026, 11, 2, 0, 0), paris(2026, 11, 9, 0, 0))

    def test_three_evenings_a_week(self):
        """An invented schedule: the evenings ticked are Monday, Wednesday and
        Saturday, at « 00:00 02:00 »; the sends are Tuesday, Thursday and
        Sunday at 00:00 and 02:00."""
        days = schedule.parse_weekdays(["0", "2", "5"])
        self.assertEqual(schedule.weekdays_value(days), "0,2,5")
        instants = schedule.reminder_instants(days, schedule.parse_times("00:00 02:00"), SIX, *self.WEEK)
        self.assertEqual(
            local(instants),
            [
                "Tue 03/11 00:00",
                "Tue 03/11 02:00",
                "Thu 05/11 00:00",
                "Thu 05/11 02:00",
                "Sun 08/11 00:00",
                "Sun 08/11 02:00",
            ],
        )
        self.assertEqual(instants[0], utc(2026, 11, 2, 23, 0))
        self.assertTrue(all(dt.tzinfo is UTC for dt in instants))

    def test_a_time_at_the_night_s_end_is_the_same_day(self):
        instants = schedule.reminder_instants((5,), (time(5, 59), SIX), SIX, *self.WEEK)
        self.assertEqual(local(instants), ["Sat 07/11 06:00", "Sun 08/11 05:59"])

    def test_an_evening_time_is_the_same_day(self):
        instants = schedule.reminder_instants((5,), (time(18, 0), time(23, 30)), SIX, *self.WEEK)
        self.assertEqual(local(instants), ["Sat 07/11 18:00", "Sat 07/11 23:30"])

    def test_the_calendar_night(self):
        instants = schedule.reminder_instants((5,), (MIDNIGHT, TWO), MIDNIGHT, *self.WEEK)
        self.assertEqual(local(instants), ["Sat 07/11 00:00", "Sat 07/11 02:00"])

    def test_sunday_evening_sends_on_monday(self):
        instants = schedule.reminder_instants((6,), (TWO,), SIX, paris(2026, 11, 8, 12, 0), paris(2026, 11, 16, 12, 0))
        self.assertEqual(local(instants), ["Mon 09/11 02:00", "Mon 16/11 02:00"])

    def test_the_window_is_open_at_its_start_and_closed_at_its_end(self):
        start, end = utc(2026, 11, 3, 23, 0), utc(2026, 11, 4, 1, 0)
        instants = schedule.reminder_instants((1,), (MIDNIGHT, TWO), SIX, start, end)
        self.assertEqual(instants, [utc(2026, 11, 4, 1, 0)])

    def test_nothing_to_send(self):
        self.assertEqual(schedule.reminder_instants((), (TWO,), SIX, *self.WEEK), [])
        self.assertEqual(schedule.reminder_instants((5,), (), SIX, *self.WEEK), [])
        self.assertEqual(schedule.reminder_instants((5,), (TWO,), SIX, self.WEEK[1], self.WEEK[0]), [])

    def test_the_year_s_end(self):
        instants = schedule.reminder_instants((3,), (MIDNIGHT,), SIX, utc(2026, 12, 30, 0, 0), utc(2027, 1, 2, 0, 0))
        self.assertEqual(instants, [utc(2026, 12, 31, 23, 0)])
        self.assertEqual(schedule.describe_reminder_instant(instants[0], SIX), "ven. 01/01 à 00:00 — nuit de jeudi")


class DaylightSavingTests(SimpleTestCase):
    def test_spring_02_00_does_not_exist_and_is_sent_once_at_03_00(self):
        for saturday in ((2026, 3, 28), (2027, 3, 27)):
            with self.subTest(saturday=saturday):
                sunday = datetime(*saturday) + timedelta(days=1)
                night = (paris(*saturday, 12, 0), paris(sunday.year, sunday.month, sunday.day, 12, 0))
                instants = schedule.reminder_instants((5,), (TWO,), SIX, *night)
                # Winter time's 02:00 is 01:00 UTC, which is 03:00 summer time.
                self.assertEqual(instants, [datetime(sunday.year, sunday.month, sunday.day, 1, 0, tzinfo=UTC)])
                self.assertEqual(local(instants), [f"Sun {sunday:%d/%m} 03:00"])
                both = schedule.reminder_instants((5,), schedule.parse_times("02:00 03:00"), SIX, *night)
                self.assertEqual(both, instants)
                self.assertEqual(
                    schedule.describe_reminder_instant(instants[0], SIX),
                    f"dim. {sunday:%d/%m} à 03:00 — nuit de samedi",
                )

    def test_autumn_the_doubled_hour_is_its_first_occurrence(self):
        night = (paris(2026, 10, 24, 12, 0), paris(2026, 10, 25, 12, 0))
        instants = schedule.reminder_instants((5,), (TWO, time(2, 30)), SIX, *night)
        self.assertEqual(instants, [utc(2026, 10, 25, 0, 0), utc(2026, 10, 25, 0, 30)])
        self.assertEqual(schedule.describe_reminder_instant(instants[1], SIX), "dim. 25/10 à 02:30 — nuit de samedi")

    def test_lateness_is_measured_in_utc(self):
        """At 02:40 the second time (01:40 UTC), the 02:30 of the first time
        is 70 minutes old - past the grace. The same subtraction on Paris
        wall clocks says 10 minutes."""
        now = paris(2026, 10, 25, 2, 40, fold=1)
        self.assertEqual(now, utc(2026, 10, 25, 1, 40))
        (instant,) = schedule.reminder_instants((5,), (time(2, 30),), SIX, now - timedelta(hours=24), now)
        self.assertEqual(now - instant, timedelta(minutes=70))
        self.assertGreater(now - instant, schedule.REMINDER_GRACE)
        wall = datetime(2026, 10, 25, 2, 40, fold=1, tzinfo=PARIS) - datetime(2026, 10, 25, 2, 30, tzinfo=PARIS)
        self.assertEqual(wall, timedelta(minutes=10))
        self.assertEqual(schedule.REMINDER_GRACE, timedelta(minutes=30))

    def test_a_day_of_quarter_hours_on_both_sundays(self):
        for day, expected in (((2026, 10, 25), 96), ((2027, 3, 28), 92)):
            with self.subTest(day=day):
                start = paris(*day, 0, 0) - timedelta(seconds=1)
                end = paris(*day, 23, 59)
                instants = schedule.gather_instants((6,), MIDNIGHT, time(23, 45), 15, start, end)
                self.assertEqual(len(instants), expected)
                self.assertEqual(len(set(instants)), expected)


class GatherRangeTests(SimpleTestCase):
    def test_every_step_up_to_the_end_included(self):
        times = schedule.range_times(SIX, time(14, 0), 30)
        self.assertEqual(len(times), 17)
        self.assertEqual((times[0], times[1], times[-1]), (SIX, time(6, 30), time(14, 0)))
        self.assertEqual(schedule.range_times(SIX, time(6, 50), 30), (SIX, time(6, 30)))
        self.assertEqual(
            schedule.range_times(time(23, 0), time(23, 59), 15), (time(23, 0), time(23, 15), time(23, 30), time(23, 45))
        )

    def test_once_a_day_when_start_is_end(self):
        self.assertEqual(schedule.range_times(time(7, 0), time(7, 0), 60), (time(7, 0),))

    def test_refused_upstream_refused_here_too(self):
        with self.assertRaises(ValueError):
            schedule.range_times(time(14, 0), SIX, 30)
        with self.assertRaises(ValueError):
            schedule.range_times(SIX, time(14, 0), 0)

    def test_gather_days_are_calendar_days(self):
        week = (paris(2026, 11, 2, 0, 0) - timedelta(seconds=1), paris(2026, 11, 8, 23, 59))
        instants = schedule.gather_instants((0, 2, 4), SIX, time(14, 0), 30, *week)
        self.assertEqual(len(instants), 3 * 17)
        self.assertEqual(instants[0], utc(2026, 11, 2, 5, 0))
        self.assertEqual(local(instants[-1:]), ["Fri 06/11 14:00"])
        self.assertEqual(schedule.gather_instants((), SIX, time(14, 0), 30, *week), [])


class CalendarInstantsTests(SimpleTestCase):
    """The automatic sales imports' days × times: calendar days, no night."""

    def test_a_time_before_six_stays_on_its_own_day(self):
        week = (paris(2026, 11, 2, 0, 0) - timedelta(seconds=1), paris(2026, 11, 8, 23, 59))
        instants = schedule.calendar_instants((2,), (TWO, time(7, 0)), *week)
        self.assertEqual(local(instants), ["Wed 04/11 02:00", "Wed 04/11 07:00"])

    def test_the_spring_gap_is_sent_once(self):
        night = (paris(2027, 3, 28, 0, 0), paris(2027, 3, 28, 12, 0))
        instants = schedule.calendar_instants((6,), (time(2, 30), time(3, 30)), *night)
        self.assertEqual(local(instants), ["Sun 28/03 03:30"])

    def test_the_next_ones(self):
        now = paris(2026, 11, 4, 7, 30)
        self.assertEqual(
            local(schedule.next_calendar_instants((2, 3), (time(7, 0),), now, count=3)),
            ["Thu 05/11 07:00", "Wed 11/11 07:00", "Thu 12/11 07:00"],
        )


class NextInstantsTests(SimpleTestCase):
    def test_the_next_reminders(self):
        now = utc(2026, 11, 2, 12, 0)
        instants = schedule.next_reminder_instants((0, 2, 5), (MIDNIGHT, TWO), SIX, now)
        self.assertEqual(
            local(instants),
            ["Tue 03/11 00:00", "Tue 03/11 02:00", "Thu 05/11 00:00", "Thu 05/11 02:00", "Sun 08/11 00:00"],
        )

    def test_strictly_after_now(self):
        now = utc(2026, 11, 3, 23, 0)
        self.assertEqual(
            schedule.next_reminder_instants((1,), (MIDNIGHT,), SIX, now, count=1), [now + timedelta(days=7)]
        )

    def test_one_a_week_still_gives_five(self):
        instants = schedule.next_reminder_instants((5,), (TWO,), SIX, utc(2026, 11, 2, 12, 0))
        self.assertEqual(len(instants), 5)
        self.assertEqual(instants[-1] - instants[0], timedelta(days=28))

    def test_nothing_to_send_nothing_next(self):
        self.assertEqual(schedule.next_reminder_instants((), (TWO,), SIX, utc(2026, 11, 2, 12, 0)), [])

    def test_the_next_gathers(self):
        now = utc(2026, 11, 2, 12, 50)
        instants = schedule.next_gather_instants((0, 2, 4), SIX, time(14, 0), 30, now, count=4)
        self.assertEqual(local(instants), ["Mon 02/11 14:00", "Wed 04/11 06:00", "Wed 04/11 06:30", "Wed 04/11 07:00"])


class PreviewTests(SimpleTestCase):
    def test_a_night_send_names_its_evening(self):
        self.assertEqual(
            schedule.describe_reminder_instant(utc(2026, 10, 4, 0, 0), SIX), "dim. 04/10 à 02:00 — nuit de samedi"
        )

    def test_a_send_on_its_own_evening_is_just_the_moment(self):
        self.assertEqual(schedule.describe_reminder_instant(utc(2026, 10, 7, 16, 0), SIX), "mer. 07/10 à 18:00")

    def test_the_calendar_night_never_names_an_evening(self):
        self.assertEqual(schedule.describe_reminder_instant(utc(2026, 10, 4, 0, 0), MIDNIGHT), "dim. 04/10 à 02:00")

    def test_a_gather(self):
        self.assertEqual(schedule.describe_gather_instant(utc(2026, 10, 7, 4, 30)), "mer. 07/10 à 06:30")

    def test_the_form_s_evening_labels(self):
        both = (MIDNIGHT, TWO)
        self.assertEqual(schedule.evening_label(5, both, SIX), "samedi soir → dim. 00:00 et 02:00")
        self.assertEqual(schedule.evening_label(5, both, MIDNIGHT), "samedi → sam. 00:00 et 02:00")
        self.assertEqual(schedule.evening_label(5, (TWO, time(22, 0)), SIX), "samedi soir → sam. 22:00 puis dim. 02:00")
        self.assertEqual(schedule.evening_label(6, (time(1, 0),), SIX), "dimanche soir → lun. 01:00")
        self.assertEqual(schedule.evening_label(1, (), SIX), "mardi soir")
        self.assertEqual(schedule.day_label(0, SIX), "lundi soir")
        self.assertEqual(schedule.sends_label(5, both, SIX), "dim. 00:00 et 02:00")
