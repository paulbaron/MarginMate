"""Writing a month: the form's post, « du … au … », the holidays, back to
the typical week."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.http import QueryDict
from django.test import TestCase

from staff.models import Timesheet, TimesheetDay
from staff.tests.support import employee, fields_as_drawn
from staff.timesheet import (
    PostedDay,
    apply_range,
    mark_holidays_off,
    month_sheet,
    read_posted_month,
    reset_to_typical_week,
    save_month,
)

JUNE = date(2026, 6, 1)   # Monday 1 to Tuesday 30, no public holiday
MAY = date(2026, 5, 1)    # 1st and 8th (Fridays), Ascension Thursday 14th, Whit Monday 25th


def stored(person, month):
    """day number → (kind, hours, note) as the database holds them."""
    return {
        row.date.day: (row.kind, row.hours, row.note)
        for row in TimesheetDay.objects.filter(timesheet__employee=person, timesheet__month=month)
    }


class ReadPostedMonthTests(TestCase):
    def test_what_a_page_drawing_the_month_posts_reads_back_as_the_month(self):
        person = employee()
        posted = read_posted_month(JUNE, fields_as_drawn(month_sheet(person, JUNE)))
        self.assertTrue(posted.is_valid)
        self.assertEqual(len(posted.days), 30)
        self.assertEqual(
            posted.days[date(2026, 6, 2)], PostedDay(date(2026, 6, 2), Decimal("7.5"), "travail", "", "7,5")
        )
        self.assertEqual(posted.days[date(2026, 6, 1)], PostedDay(date(2026, 6, 1), Decimal("0"), "repos", "", ""))

    def test_a_querydict_s_last_value_and_the_whitespace_of_a_note(self):
        data = QueryDict(mutable=True)
        data.setlist("heures-2026-06-02", ["3", "7h30"])
        data["note-2026-06-02"] = "  arrivé \t en   retard "
        day = read_posted_month(JUNE, data).days[date(2026, 6, 2)]
        self.assertEqual((day.hours, day.note, day.kind), (Decimal("7.5"), "arrivé en retard", None))

    def test_a_field_not_posted_is_none_not_blank(self):
        """A disabled input is not posted at all: missing keeps the day's
        value, blank means 0."""
        posted = read_posted_month(JUNE, {"motif-2026-06-02": "conges", "heures-2026-06-03": ""})
        self.assertEqual(posted.days[date(2026, 6, 2)], PostedDay(date(2026, 6, 2), None, "conges", None, None))
        self.assertEqual(posted.days[date(2026, 6, 3)].hours, Decimal("0"))
        self.assertNotIn(date(2026, 6, 4), posted.days)

    def test_a_field_for_a_day_outside_the_month_is_never_read(self):
        posted = read_posted_month(
            JUNE, {"heures-2026-07-01": "abc", "heures-2026-05-31": "8", "heures-2026-06-31": "8"}
        )
        self.assertEqual(posted.days, {})
        self.assertTrue(posted.is_valid)

    def test_every_problem_is_a_french_sentence_naming_its_day(self):
        posted = read_posted_month(
            JUNE,
            {
                "heures-2026-06-04": "abc",
                "heures-2026-06-05": "-3",
                "heures-2026-06-06": "25",
                "motif-2026-06-09": "vacances",
                "note-2026-06-10": "x" * 256,
            },
        )
        self.assertFalse(posted.is_valid)
        self.assertEqual(len(posted.errors), 5)
        self.assertTrue(posted.errors[0].startswith("Jeudi 4 : « abc »"), posted.errors[0])
        self.assertEqual(posted.errors[1], "Vendredi 5 : les heures ne peuvent pas être négatives.")
        self.assertTrue(posted.errors[2].startswith("Samedi 6 : "))
        self.assertEqual(posted.errors[3], "Mardi 9 : motif inconnu « vacances ».")
        self.assertEqual(posted.errors[4], "Mercredi 10 : la note dépasse 255 caractères.")
        self.assertEqual(posted.days[date(2026, 6, 4)].error, posted.errors[0])
        self.assertEqual(posted.days[date(2026, 6, 4)].hours_text, "abc")
        self.assertIsNone(posted.days[date(2026, 6, 4)].hours)

    def test_two_figures_a_space_apart_are_refused_naming_the_day(self):
        """« 1 5 » on Tuesday 2 was stored as 15 h."""
        posted = read_posted_month(JUNE, {"heures-2026-06-02": "1 5", "heures-2026-06-03": "0 5"})
        self.assertFalse(posted.is_valid)
        self.assertTrue(posted.errors[0].startswith("Mardi 2 : « 1 5 » : un espace entre deux chiffres"))
        self.assertTrue(posted.errors[1].startswith("Mercredi 3 : « 0 5 »"))
        self.assertIsNone(posted.days[date(2026, 6, 2)].hours)

    def test_a_refused_post_draws_back_as_it_was_typed(self):
        person = employee()
        data = fields_as_drawn(month_sheet(person, JUNE))
        data["heures-2026-06-04"] = "7h7"
        data["heures-2026-06-03"] = "5"
        posted = read_posted_month(JUNE, data)
        sheet = month_sheet(person, JUNE, posted)
        wednesday, thursday = sheet.days[2], sheet.days[3]
        self.assertEqual((thursday.hours_input, thursday.hours), ("7h7", Decimal("7.5")))
        self.assertTrue(thursday.error.startswith("Jeudi 4 : "))
        self.assertEqual((wednesday.hours_input, wednesday.hours, wednesday.error), ("5", Decimal("5"), ""))
        self.assertEqual(sheet.errors, posted.errors)
        self.assertFalse(sheet.saved)
        self.assertFalse(Timesheet.objects.exists())


class SaveMonthTests(TestCase):
    def setUp(self):
        self.person = employee()

    def save(self, changes, month=JUNE):
        """Post the month as the page draws it, with `changes` on top."""
        data = fields_as_drawn(month_sheet(self.person, month))
        data.update(changes)
        posted = read_posted_month(month, data)
        self.assertTrue(posted.is_valid, posted.errors)
        return save_month(self.person, month, posted.days)

    def test_the_first_save_writes_every_day_of_the_month(self):
        outcome = self.save({})
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.changed, ())
        self.assertEqual(len(outcome.touched), 30)
        self.assertEqual(outcome.timesheet.month, JUNE)
        days = stored(self.person, JUNE)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[2], ("travail", Decimal("7.5"), ""))
        self.assertEqual(days[7], ("repos", Decimal("0"), ""))

    def test_saving_the_same_thing_twice_changes_nothing(self):
        self.save({"heures-2026-06-02": "6,5", "motif-2026-06-09": "conges", "note-2026-06-03": "livraison"})
        before = stored(self.person, JUNE)
        rows = list(TimesheetDay.objects.values_list("pk", flat=True).order_by("pk"))
        outcome = self.save({})
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.changed, ())
        self.assertEqual(stored(self.person, JUNE), before)
        self.assertEqual(list(TimesheetDay.objects.values_list("pk", flat=True).order_by("pk")), rows)
        self.assertEqual(Timesheet.objects.count(), 1)

    def test_what_changed_is_said(self):
        """Thursday 4's 7,5 h typed again as « 7h30 » is no change."""
        self.save({})
        outcome = self.save({"heures-2026-06-02": "6", "note-2026-06-03": "livraison", "heures-2026-06-04": "7h30"})
        self.assertEqual(outcome.changed, (date(2026, 6, 2), date(2026, 6, 3)))
        self.assertEqual(stored(self.person, JUNE)[3], ("travail", Decimal("6"), "livraison"))

    def test_the_last_save_is_dated(self):
        first = self.save({}).timesheet.updated_at
        second = self.save({}).timesheet
        second.refresh_from_db()
        self.assertGreaterEqual(second.updated_at, first)

    def test_an_absence_is_zero_hours_whatever_was_posted(self):
        """Without JavaScript, the hours field still says 7 when « Congés
        payés » is chosen."""
        self.save({"motif-2026-06-02": "conges", "motif-2026-06-03": "maladie", "heures-2026-06-03": "12"})
        days = stored(self.person, JUNE)
        self.assertEqual(days[2], ("conges", Decimal("0"), ""))
        self.assertEqual(days[3], ("maladie", Decimal("0"), ""))
        self.assertEqual(month_sheet(self.person, JUNE).weeks[0].total, Decimal("36") - Decimal("7.5") - 6)

    def test_hours_typed_on_a_day_of_rest_mean_he_worked_it(self):
        outcome = self.save({"heures-2026-06-07": "4"})
        self.assertEqual(stored(self.person, JUNE)[7], ("travail", Decimal("4"), ""))
        self.assertEqual(outcome.adjustments, ("Dimanche 7 : 4 h saisies sur un jour de repos, comptées en Travail.",))
        self.assertEqual(outcome.changed, (date(2026, 6, 7),))

    def test_a_day_just_switched_to_rest_is_a_day_off_whatever_its_field_still_says(self):
        outcome = self.save({"motif-2026-06-02": "repos"})
        self.assertEqual(stored(self.person, JUNE)[2], ("repos", Decimal("0"), ""))
        self.assertEqual(outcome.adjustments, ())

    def test_a_worked_day_at_zero_stays_worked(self):
        self.save({"heures-2026-06-02": ""})
        self.assertEqual(stored(self.person, JUNE)[2], ("travail", Decimal("0"), ""))
        self.assertEqual(month_sheet(self.person, JUNE).days[1].hours_text, "0")

    def test_a_field_not_posted_keeps_the_day_s_value(self):
        self.save({"heures-2026-06-02": "5", "note-2026-06-02": "réunion"})
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), kind="travail")])
        self.assertEqual(stored(self.person, JUNE)[2], ("travail", Decimal("5"), "réunion"))
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 3), note="")])
        self.assertEqual(stored(self.person, JUNE)[3], ("travail", Decimal("6"), ""))

    def test_back_to_work_without_hours_takes_the_typical_week_s(self):
        """The hours field disabled while « Congés payés » was chosen is not
        posted when the day goes back to « Travail »."""
        self.save({"motif-2026-06-02": "conges"})
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), kind="travail")])
        self.assertEqual(stored(self.person, JUNE)[2], ("travail", Decimal("7.5"), ""))

    def test_a_day_outside_the_month_is_refused_and_nothing_is_written(self):
        for day in (date(2026, 7, 1), date(2026, 5, 31)):
            with self.subTest(day=day), self.assertRaises(ValueError):
                save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("5")), PostedDay(day)])
        self.assertFalse(Timesheet.objects.exists())

    def test_whatever_a_caller_hands_in_is_checked_before_anything_is_written(self):
        for posted in (
            PostedDay(date(2026, 6, 2), hours=Decimal("25")),
            PostedDay(date(2026, 6, 2), hours=Decimal("-1")),
            PostedDay(date(2026, 6, 2), hours=Decimal("7.555")),
            PostedDay(date(2026, 6, 2), kind="vacances"),
            PostedDay(date(2026, 6, 2), note="x" * 256),
            PostedDay(date(2026, 6, 2), error="Mardi 2 : « abc » n'est pas un nombre d'heures."),
        ):
            with self.subTest(posted=posted), self.assertRaises(ValueError):
                save_month(self.person, JUNE, [posted])
        with self.assertRaises(ValueError):
            save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2)), PostedDay(date(2026, 6, 2))])
        with self.assertRaises(TypeError):
            save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=7.5)])
        self.assertFalse(Timesheet.objects.exists())

    def test_a_failure_while_writing_leaves_nothing_behind(self):
        failing = mock.patch.object(TimesheetDay.objects, "bulk_create", side_effect=RuntimeError("disque plein"))
        with failing, self.assertRaises(RuntimeError):
            self.save({})
        self.assertFalse(Timesheet.objects.exists())

    def test_the_month_is_the_one_holding_the_date_given(self):
        save_month(self.person, date(2026, 6, 17), [])
        self.assertEqual(Timesheet.objects.get().month, JUNE)

    def test_months_of_28_29_and_31_days(self):
        for month, length in ((date(2026, 2, 1), 28), (date(2028, 2, 1), 29), (date(2026, 3, 1), 31)):
            with self.subTest(month=month):
                outcome = self.save({}, month)
                self.assertEqual(len(stored(self.person, month)), length)
                self.assertEqual(outcome.timesheet.days.count(), length)


class ApplyRangeTests(TestCase):
    def setUp(self):
        self.person = employee()

    def test_on_leave_from_the_11th_to_the_18th(self):
        """Six days of leave, not eight: Sunday 14 and Monday 15 are days
        off, and a day off is no day of leave - counted as one, the sheet
        the employee signs said « Congés payés : 8 jours », which neither
        French way of counting leave gives (review, 28/09)."""
        outcome = apply_range(self.person, JUNE, date(2026, 6, 11), date(2026, 6, 18), "conges")
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.touched, tuple(date(2026, 6, day) for day in range(11, 19)))
        self.assertEqual(outcome.left_alone, (date(2026, 6, 14), date(2026, 6, 15)))
        days = stored(self.person, JUNE)
        self.assertEqual([days[day][0] for day in range(11, 19)], ["conges"] * 3 + ["repos"] * 2 + ["conges"] * 3)
        self.assertEqual({days[day][1] for day in range(11, 19)}, {Decimal("0")})
        self.assertEqual(days[10], ("travail", Decimal("6"), ""))
        self.assertEqual(days[19], ("travail", Decimal("7.5"), ""))
        self.assertEqual(len(days), 30)
        self.assertEqual(month_sheet(self.person, JUNE).summary.absences[0].days, 6)
        self.assertEqual(len(outcome.changed), 6)

    def test_a_day_made_a_day_off_this_month_stays_one(self):
        """Thursday 11 switched to « Repos » (a day swapped for the Sunday
        worked): leave over it leaves it a day off."""
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 11), kind="repos")])
        outcome = apply_range(self.person, JUNE, date(2026, 6, 11), date(2026, 6, 12), "maladie")
        self.assertEqual(outcome.left_alone, (date(2026, 6, 11),))
        self.assertEqual((stored(self.person, JUNE)[11][0], stored(self.person, JUNE)[12][0]), ("repos", "maladie"))

    def test_a_day_off_of_the_typical_week_worked_this_month_is_left_as_worked(self):
        """Sunday 14 was worked, 4 h: leave written over the week does not
        turn hours somebody typed into a day of leave on a Sunday."""
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 14), hours=Decimal("4"))])
        outcome = apply_range(self.person, JUNE, date(2026, 6, 13), date(2026, 6, 16), "conges")
        self.assertEqual(outcome.left_alone, (date(2026, 6, 14), date(2026, 6, 15)))
        days = stored(self.person, JUNE)
        self.assertEqual([days[day][:2] for day in (13, 14, 15, 16)], [
            ("conges", Decimal("0")), ("travail", Decimal("4")), ("repos", Decimal("0")), ("conges", Decimal("0")),
        ])

    def test_a_range_of_days_off_only_writes_nothing(self):
        outcome = apply_range(self.person, JUNE, date(2026, 6, 14), date(2026, 6, 15), "conges")
        self.assertEqual((outcome.timesheet, outcome.created, outcome.changed), (None, False, ()))
        self.assertEqual(outcome.left_alone, (date(2026, 6, 14), date(2026, 6, 15)))
        self.assertFalse(Timesheet.objects.exists())

    def test_rest_over_a_range_is_every_day_of_it(self):
        """« Repos » is the one kind days off already are - and a worked
        Sunday under it is a day off again: it does what it says."""
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 14), hours=Decimal("4"))])
        outcome = apply_range(self.person, JUNE, date(2026, 6, 12), date(2026, 6, 15), "repos")
        self.assertEqual(outcome.left_alone, ())
        self.assertEqual({stored(self.person, JUNE)[day][0] for day in (12, 13, 14, 15)}, {"repos"})

    def test_a_range_over_the_month_s_edge_is_cut_to_the_month(self):
        outcome = apply_range(self.person, JUNE, date(2026, 5, 28), date(2026, 6, 3), "maladie")
        self.assertEqual(outcome.touched, (date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)))
        self.assertEqual(outcome.changed, (date(2026, 6, 2), date(2026, 6, 3)))   # Monday 1 is a day off
        outcome = apply_range(self.person, JUNE, date(2026, 6, 29), date(2026, 7, 5), "conges")
        self.assertEqual(outcome.touched, (date(2026, 6, 29), date(2026, 6, 30)))
        self.assertEqual(Timesheet.objects.count(), 1)
        self.assertEqual(stored(self.person, JUNE)[30], ("conges", Decimal("0"), ""))

    def test_dates_the_wrong_way_round_are_swapped(self):
        outcome = apply_range(self.person, JUNE, date(2026, 6, 18), date(2026, 6, 11), "conges")
        self.assertEqual(len(outcome.touched), 8)

    def test_a_single_day(self):
        outcome = apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 2), "repos_comp")
        self.assertEqual(outcome.touched, (date(2026, 6, 2),))
        self.assertEqual(stored(self.person, JUNE)[2], ("repos_comp", Decimal("0"), ""))

    def test_a_range_with_no_day_in_the_month_is_refused_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            apply_range(self.person, JUNE, date(2026, 7, 1), date(2026, 7, 5), "conges")
        with self.assertRaises(ValueError):
            apply_range(self.person, JUNE, None, date(2026, 6, 5), "conges")
        with self.assertRaises(ValueError):
            apply_range(self.person, JUNE, date(2026, 6, 1), date(2026, 6, 5), "vacances")
        with self.assertRaises(ValueError):
            apply_range(self.person, JUNE, date(2026, 6, 1), date(2026, 6, 5), "travail", hours=Decimal("25"))
        self.assertFalse(Timesheet.objects.exists())

    def test_work_with_hours_puts_them_on_every_working_day(self):
        """Not on the Sunday and Monday off: « Travail 6 h du 1er au 30 »
        was adding 6 h to each of the month's nine days off."""
        outcome = apply_range(self.person, JUNE, date(2026, 6, 5), date(2026, 6, 9), "travail", hours=Decimal("6"))
        self.assertEqual(outcome.left_alone, (date(2026, 6, 7), date(2026, 6, 8)))
        days = stored(self.person, JUNE)
        self.assertEqual(
            [days[day] for day in (5, 6, 7, 8, 9)],
            [("travail", Decimal("6"), "")] * 2 + [("repos", Decimal("0"), "")] * 2 + [("travail", Decimal("6"), "")],
        )

    def test_work_without_hours_is_the_typical_week_again(self):
        """A day off stays « Repos » rather than printing « 0 » on every Sunday."""
        apply_range(self.person, JUNE, date(2026, 6, 1), date(2026, 6, 14), "conges")
        apply_range(self.person, JUNE, date(2026, 6, 1), date(2026, 6, 14), "travail")
        days = stored(self.person, JUNE)
        self.assertEqual(days[1], ("repos", Decimal("0"), ""))
        self.assertEqual(days[2], ("travail", Decimal("7.5"), ""))
        self.assertEqual(days[3], ("travail", Decimal("6"), ""))
        self.assertEqual(days[7], ("repos", Decimal("0"), ""))

    def test_an_absence_ignores_the_hours_given(self):
        apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "maladie", hours=Decimal("4"))
        self.assertEqual(stored(self.person, JUNE)[2], ("maladie", Decimal("0"), ""))

    def test_a_note_given_is_written_one_not_given_goes_with_a_change_of_kind(self):
        self.save_notes()
        apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "travail", hours=Decimal("5"))
        days = stored(self.person, JUNE)
        self.assertEqual(days[2][2], "arrivé en retard")   # still « Travail »: kept
        apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "conges")
        self.assertEqual(stored(self.person, JUNE)[2][2], "")   # now on leave: gone
        apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "conges", note="  congés  d'été ")
        self.assertEqual({stored(self.person, JUNE)[day][2] for day in (2, 3)}, {"congés d'été"})

    def save_notes(self):
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), note="arrivé en retard")])

    def test_a_range_on_a_saved_month_leaves_the_other_days_as_saved(self):
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 20), hours=Decimal("10"))])
        outcome = apply_range(self.person, JUNE, date(2026, 6, 2), date(2026, 6, 3), "conges")
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.changed, (date(2026, 6, 2), date(2026, 6, 3)))
        self.assertEqual(stored(self.person, JUNE)[20], ("travail", Decimal("10"), ""))


class HolidaysOffTests(TestCase):
    def setUp(self):
        self.person = employee()

    def test_every_holiday_of_the_month_on_a_working_day_becomes_a_day_off(self):
        """Whit Monday falls on a Monday, a day off: it stays « Repos »,
        blank on the sheet, and is not counted - « Férié chômé : 3 jours »,
        not 4 (review, 28/09)."""
        outcome = mark_holidays_off(self.person, MAY)
        self.assertEqual(outcome.touched, (date(2026, 5, 1), date(2026, 5, 8), date(2026, 5, 14), date(2026, 5, 25)))
        self.assertEqual(outcome.left_alone, (date(2026, 5, 25),))
        days = stored(self.person, MAY)
        self.assertEqual({days[day] for day in (1, 8, 14)}, {("ferie", Decimal("0"), "")})
        self.assertEqual(days[25], ("repos", Decimal("0"), ""))
        self.assertEqual(days[2], ("travail", Decimal("7.5"), ""))
        self.assertEqual(outcome.changed, (date(2026, 5, 1), date(2026, 5, 8), date(2026, 5, 14)))
        sheet = month_sheet(self.person, MAY)
        self.assertEqual(sheet.days[0].note_text, "Férié chômé — Fête du Travail")
        self.assertEqual([(count.label, count.days) for count in sheet.summary.absences], [("Férié chômé", 3)])
        # A holiday off is no shortfall either.
        self.assertEqual(sheet.summary.absence_hours, Decimal("22.5"))
        self.assertEqual(sheet.summary.difference, Decimal("0"))

    def test_twice_changes_nothing_the_second_time(self):
        mark_holidays_off(self.person, MAY)
        self.assertEqual(mark_holidays_off(self.person, MAY).changed, ())

    def test_a_month_with_no_holiday_writes_nothing(self):
        outcome = mark_holidays_off(self.person, JUNE)
        self.assertEqual((outcome.timesheet, outcome.created, outcome.touched), (None, False, ()))
        self.assertFalse(Timesheet.objects.exists())

    def test_a_month_whose_holidays_all_fall_on_days_off_writes_nothing(self):
        """April 2026: Easter Monday, a Monday."""
        outcome = mark_holidays_off(self.person, date(2026, 4, 1))
        self.assertEqual((outcome.timesheet, outcome.created, outcome.changed), (None, False, ()))
        self.assertEqual(outcome.left_alone, (date(2026, 4, 6),))
        self.assertFalse(Timesheet.objects.exists())

    def test_a_holiday_worked_on_a_day_off_stays_worked(self):
        """Whit Monday worked, 5 h: the owner's decision, as for any holiday."""
        save_month(self.person, MAY, [PostedDay(date(2026, 5, 25), hours=Decimal("5"))])
        outcome = mark_holidays_off(self.person, MAY)
        self.assertEqual(outcome.left_alone, (date(2026, 5, 25),))
        self.assertEqual(stored(self.person, MAY)[25], ("travail", Decimal("5"), ""))


class ResetToTypicalWeekTests(TestCase):
    def setUp(self):
        self.person = employee()

    def test_a_saved_month_goes_back_to_the_typical_week(self):
        save_month(
            self.person,
            JUNE,
            [
                PostedDay(date(2026, 6, 2), hours=Decimal("3"), note="rendez-vous"),
                PostedDay(date(2026, 6, 7), hours=Decimal("5")),
            ],
        )
        apply_range(self.person, JUNE, date(2026, 6, 15), date(2026, 6, 20), "conges")
        self.person.saturday_hours = Decimal("6")
        self.person.save()
        outcome = reset_to_typical_week(self.person, JUNE)
        self.assertFalse(outcome.created)
        self.assertEqual(len(outcome.touched), 30)
        days = stored(self.person, JUNE)
        self.assertEqual(days[2], ("travail", Decimal("7.5"), ""))
        self.assertEqual(days[7], ("repos", Decimal("0"), ""))
        self.assertEqual(days[16], ("travail", Decimal("7.5"), ""))
        self.assertEqual(days[6], ("travail", Decimal("6"), ""))   # today's typical week, not June's old one
        self.assertTrue(month_sheet(self.person, JUNE).saved)

    def test_an_unsaved_month_already_is_the_typical_week(self):
        outcome = reset_to_typical_week(self.person, JUNE)
        self.assertEqual((outcome.timesheet, outcome.changed), (None, ()))
        self.assertFalse(Timesheet.objects.exists())
