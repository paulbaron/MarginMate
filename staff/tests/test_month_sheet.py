"""The month as the page and the PDF draw it: its days, its weeks and their
totals, its summary. Built from an unsaved employee - no database - except
where what is read is the saved sheet."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from staff.models import Timesheet
from staff.tests.support import employee
from staff.timesheet import (
    DayEntry,
    PostedDay,
    apply_range,
    build_sheet,
    mark_holidays_off,
    month_days,
    month_sheet,
    planned_days,
    reset_to_typical_week,
    save_month,
    saved_month_sheets,
)

JUNE = date(2026, 6, 1)  # starts on a Monday, 30 days
MARCH = date(2026, 3, 1)  # starts on a Sunday, 31 days: six week totals, the most a month can have
FEBRUARY = date(2026, 2, 1)  # starts on a Sunday, 28 days
MAY = date(2026, 5, 1)  # four public holidays


def totals(sheet):
    return [week.total for week in sheet.weeks]


class PlannedDaysTests(SimpleTestCase):
    def test_the_typical_week_over_the_month(self):
        days = planned_days(employee(save=False), JUNE)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[0], DayEntry(date(2026, 6, 1), Decimal("0"), "repos"))
        self.assertEqual(days[1], DayEntry(date(2026, 6, 2), Decimal("7.5"), "travail"))
        self.assertEqual(days[2], DayEntry(date(2026, 6, 3), Decimal("6"), "travail"))
        self.assertEqual(days[6], DayEntry(date(2026, 6, 7), Decimal("0"), "repos"))

    def test_public_holidays_are_not_zeroed(self):
        """1 May 2026 is a Friday: worked or not is the owner's decision."""
        first_of_may = planned_days(employee(save=False), MAY)[0]
        self.assertEqual((first_of_may.kind, first_of_may.hours), ("travail", Decimal("7.5")))

    def test_no_typical_week_is_a_month_of_rest(self):
        days = planned_days(employee(save=False, monday_hours=Decimal("0")), JUNE)
        self.assertEqual({day.kind for day in days}, {"repos"})


class PlannedSheetTests(SimpleTestCase):
    def test_june_2026(self):
        sheet = build_sheet(employee(save=False), JUNE)
        self.assertFalse(sheet.saved)
        self.assertEqual(sheet.title, "Mois de juin 2026")
        self.assertEqual(sheet.label, "juin 2026")
        self.assertEqual(sheet.slug, "2026-06")
        self.assertEqual(sheet.employee_name, "DUPONT Jeanne")
        self.assertEqual((sheet.previous_month, sheet.next_month), (date(2026, 5, 1), date(2026, 7, 1)))
        self.assertEqual([day.name for day in sheet.days][:3], ["Lundi 1", "Mardi 2", "Mercredi 3"])
        self.assertEqual(sheet.days[-1].name, "Mardi 30")
        self.assertEqual([day.hours_text for day in sheet.days[:7]], ["", "7,5", "6", "7,5", "7,5", "7,5", ""])
        self.assertEqual(totals(sheet), [Decimal("36")] * 4 + [Decimal("7.5")])
        self.assertEqual([week.partial for week in sheet.weeks], [False] * 4 + [True])
        self.assertEqual(sheet.weeks[0].label, "Total semaine")
        self.assertEqual(sheet.weeks[-1].label, "Total (semaine incomplète)")
        self.assertEqual((sheet.weeks[-1].first, sheet.weeks[-1].last), (date(2026, 6, 29), date(2026, 6, 30)))
        self.assertEqual(sheet.weeks[-1].total_text, "7,5")
        self.assertEqual(sheet.weekly_hours_text, "36")

    def test_the_summary_of_a_typical_month(self):
        summary = build_sheet(employee(save=False), JUNE).summary
        # Four weeks of 36 h, and Tuesday 30.
        self.assertEqual(summary.worked_hours, Decimal("151.5"))
        self.assertEqual(summary.planned_hours, Decimal("151.5"))
        self.assertEqual(summary.difference_text, "0 h")
        self.assertEqual(summary.days_worked, 21)
        self.assertEqual(summary.days_worked_text, "21 jours")
        self.assertEqual(summary.absences, ())

    def test_a_week_straddling_two_months_has_a_partial_total_in_each(self):
        """Week of Monday 29 June to Sunday 5 July: 7,5 h in June (Tuesday
        30), 28,5 h in July (Wednesday 1 to Saturday 4)."""
        person = employee(save=False)
        june, july = build_sheet(person, JUNE), build_sheet(person, date(2026, 7, 1))
        self.assertEqual(june.weeks[-1].monday, july.weeks[0].monday)
        self.assertEqual(july.weeks[0].monday, date(2026, 6, 29))
        self.assertEqual(june.weeks[-1].total + july.weeks[0].total, Decimal("36"))
        self.assertEqual((june.weeks[-1].total, july.weeks[0].total), (Decimal("7.5"), Decimal("28.5")))
        self.assertTrue(june.weeks[-1].partial and july.weeks[0].partial)

    def test_the_worst_case_a_31_day_month_starting_on_a_sunday(self):
        sheet = build_sheet(employee(save=False), MARCH)
        self.assertEqual(len(sheet.days), 31)
        self.assertEqual(len(sheet.weeks), 6)
        self.assertEqual([len(week.days) for week in sheet.weeks], [1, 7, 7, 7, 7, 2])
        self.assertEqual(totals(sheet), [Decimal("0")] + [Decimal("36")] * 4 + [Decimal("7.5")])
        self.assertEqual(sheet.weeks[0].first, sheet.weeks[0].last)
        self.assertEqual(sheet.weeks[0].monday, date(2026, 2, 23))

    def test_a_partial_week_can_add_up_to_a_full_one(self):
        """February 2026 ends on a Saturday: Monday 23 to Saturday 28 is
        partial and still holds the whole 36 h - partial means the month's
        edge, not fewer hours."""
        sheet = build_sheet(employee(save=False), FEBRUARY)
        self.assertEqual(len(sheet.days), 28)
        self.assertEqual([week.partial for week in sheet.weeks], [True, False, False, False, True])
        self.assertEqual(totals(sheet), [Decimal("0")] + [Decimal("36")] * 4)

    def test_a_leap_february(self):
        sheet = build_sheet(employee(save=False), date(2028, 2, 1))
        self.assertEqual(len(sheet.days), 29)
        self.assertEqual(sheet.days[-1].name, "Mardi 29")
        self.assertEqual(sheet.days[-1].hours, Decimal("7.5"))
        self.assertEqual(len(build_sheet(employee(save=False), date(2100, 2, 1)).days), 28)

    def test_every_month_s_weeks_add_up_to_the_month(self):
        """Over seven years: every day in exactly one week, weeks Monday to
        Sunday, only the first and last partial, and the week totals summing
        to the month's hours."""
        person = employee(save=False, monday_hours=Decimal("3.5"), sunday_hours=Decimal("0.25"))
        for year in range(2024, 2031):
            for number in range(1, 13):
                month = date(year, number, 1)
                with self.subTest(month=month):
                    sheet = build_sheet(person, month)
                    self.assertEqual([day.date for week in sheet.weeks for day in week.days], month_days(month))
                    for week in sheet.weeks:
                        self.assertEqual(week.days[-1].date - week.days[0].date, timedelta(days=len(week.days) - 1))
                        self.assertTrue(week.days[0].date.weekday() == 0 or week is sheet.weeks[0])
                        self.assertTrue(week.days[-1].date.weekday() == 6 or week is sheet.weeks[-1])
                    self.assertTrue(all(not week.partial for week in sheet.weeks[1:-1]))
                    self.assertEqual(sum(totals(sheet), Decimal("0")), sheet.summary.worked_hours)

    def test_holidays_are_named_and_left_as_the_typical_week_says(self):
        sheet = build_sheet(employee(save=False), MAY)
        first_of_may = sheet.days[0]
        self.assertEqual(first_of_may.holiday, "Fête du Travail")
        self.assertEqual(first_of_may.holiday_label, "Férié : Fête du Travail")
        self.assertEqual((first_of_may.kind, first_of_may.hours_text), ("travail", "7,5"))
        self.assertFalse(first_of_may.differs)
        self.assertEqual([day.date.day for day in sheet.holidays], [1, 8, 14, 25])
        self.assertEqual(sheet.days[1].holiday_label, "")

    def test_each_day_carries_its_typical_hours_and_its_fields(self):
        tuesday = build_sheet(employee(save=False), JUNE).days[1]
        self.assertEqual((tuesday.typical_hours, tuesday.typical_hours_text), (Decimal("7.5"), "7,5"))
        self.assertEqual(tuesday.weekday_name, "Mardi")
        self.assertEqual(tuesday.iso, "2026-06-02")
        self.assertEqual(
            (tuesday.hours_field, tuesday.kind_field, tuesday.note_field),
            ("heures-2026-06-02", "motif-2026-06-02", "note-2026-06-02"),
        )
        self.assertEqual(tuesday.hours_input, "7,5")
        self.assertEqual(tuesday.label, "Travail")


class CorrectedSheetTests(SimpleTestCase):
    """A month whose days are not all the typical week's."""

    def setUp(self):
        self.sheet = build_sheet(
            employee(save=False),
            MAY,
            [
                DayEntry(date(2026, 5, 1), Decimal("0"), "ferie"),
                DayEntry(date(2026, 5, 5), Decimal("0"), "conges", "pont"),
                DayEntry(date(2026, 5, 6), Decimal("0"), "conges"),
                DayEntry(date(2026, 5, 7), Decimal("4"), "travail", "demi-journée de congés"),
                DayEntry(date(2026, 5, 8), Decimal("0"), "travail"),
                DayEntry(date(2026, 5, 10), Decimal("5.5"), "travail"),
                DayEntry(date(2026, 5, 12), Decimal("0"), "maladie"),
                DayEntry(date(2026, 5, 17), Decimal("0"), "repos", "fermeture"),
            ],
        )
        self.days = {day.date.day: day for day in self.sheet.days}

    def test_what_each_column_prints(self):
        self.assertEqual((self.days[1].hours_text, self.days[1].note_text), ("", "Férié chômé — Fête du Travail"))
        self.assertEqual((self.days[5].hours_text, self.days[5].note_text), ("", "Congés payés — pont"))
        self.assertEqual((self.days[6].hours_text, self.days[6].note_text), ("", "Congés payés"))
        self.assertEqual((self.days[7].hours_text, self.days[7].note_text), ("4", "demi-journée de congés"))
        self.assertEqual((self.days[8].hours_text, self.days[8].note_text), ("0", ""))
        self.assertEqual((self.days[10].hours_text, self.days[10].note_text), ("5,5", ""))
        self.assertEqual((self.days[12].hours_text, self.days[12].note_text), ("", "Arrêt maladie"))
        self.assertEqual((self.days[17].hours_text, self.days[17].note_text), ("", "fermeture"))
        self.assertEqual((self.days[3].hours_text, self.days[3].note_text), ("", ""))

    def test_what_differs_from_the_typical_week(self):
        differing = sorted(number for number, day in self.days.items() if day.differs)
        self.assertEqual(differing, [1, 5, 6, 7, 8, 10, 12])

    def test_the_hours_fields_show_the_worked_hours_only(self):
        self.assertEqual([self.days[n].hours_input for n in (1, 5, 7, 8, 10, 17)], ["", "", "4", "0", "5,5", ""])

    def test_the_week_totals_count_the_worked_hours_only(self):
        # 1-3 May: Friday off (holiday), Saturday 7,5. 4-10: Tuesday and
        # Wednesday on leave, Thursday 4, Friday 0, Saturday 7,5, Sunday 5,5.
        self.assertEqual(totals(self.sheet)[:2], [Decimal("7.5"), Decimal("17")])

    def test_the_summary(self):
        summary = self.sheet.summary
        planned = build_sheet(employee(save=False), MAY).summary.worked_hours
        self.assertEqual(summary.planned_hours, planned)
        # Lost: 7,5 (1st) + 7,5 (5th) + 6 (6th) + 3,5 (7th) + 7,5 (8th) + 7,5 (12th); won: 5,5 (10th).
        self.assertEqual(summary.worked_hours, planned - Decimal("39.5") + Decimal("5.5"))
        # Of those, the absences - the holiday, the two days of leave, the
        # sick day - planned 28,5 h: no shortfall. What is left is the
        # difference: 3,5 h short on the 7th, the 8th worked 0, 5,5 h on
        # the Sunday.
        self.assertEqual(summary.absence_hours, Decimal("28.5"))
        self.assertEqual(summary.difference, Decimal("-5.5"))
        self.assertEqual(summary.difference_text, "−5,5 h")
        self.assertEqual(summary.difference_label, "Écart hors absences")
        self.assertEqual(
            [(count.label, count.days, count.days_text) for count in summary.absences],
            [("Congés payés", 2, "2 jours"), ("Férié chômé", 1, "1 jour"), ("Arrêt maladie", 1, "1 jour")],
        )
        # The 8th is « Travail » at 0 h: not a day worked. The Sunday 10th is.
        self.assertEqual(summary.days_worked, build_sheet(employee(save=False), MAY).summary.days_worked - 5 + 1)

    def test_a_week_of_leave_is_no_shortfall(self):
        """A month worked exactly as planned but for a week of leave: the
        difference is 0 h. Said against every planned day, it was the
        leave's own hours, as a deficit, under « Lu et approuvé » (review,
        28/09)."""
        leave = [DayEntry(date(2026, 6, day), Decimal("0"), "conges") for day in range(9, 14)]
        summary = build_sheet(employee(save=False), JUNE, leave).summary
        self.assertEqual(summary.planned_hours, Decimal("151.5"))
        self.assertEqual(summary.absence_hours, Decimal("36"))
        self.assertEqual(summary.worked_hours, Decimal("115.5"))
        self.assertEqual((summary.difference, summary.difference_text), (Decimal("0"), "0 h"))

    def test_with_no_absence_the_difference_is_against_the_whole_plan(self):
        summary = build_sheet(employee(save=False), JUNE, [DayEntry(date(2026, 6, 7), Decimal("3"), "travail")]).summary
        self.assertEqual((summary.absence_hours, summary.difference), (Decimal("0"), Decimal("3")))
        self.assertEqual(summary.difference_label, "Écart")

    def test_hours_given_to_a_day_not_worked_are_never_shown_nor_counted(self):
        sheet = build_sheet(employee(save=False), JUNE, [DayEntry(date(2026, 6, 2), Decimal("7.5"), "conges")])
        self.assertEqual(sheet.days[1].hours, Decimal("0"))
        self.assertEqual(sheet.weeks[0].total, Decimal("28.5"))

    def test_a_day_outside_the_month_is_refused(self):
        with self.assertRaises(ValueError):
            build_sheet(employee(save=False), JUNE, [DayEntry(date(2026, 7, 1), Decimal("7"), "travail")])


class ReadingTheSavedMonthTests(TestCase):
    def setUp(self):
        self.person = employee()

    def test_an_unsaved_month_is_the_typical_week_in_one_query(self):
        with self.assertNumQueries(1):
            sheet = month_sheet(self.person, JUNE)
        self.assertFalse(sheet.saved)
        self.assertEqual(sheet.summary.worked_hours, Decimal("151.5"))

    def test_a_saved_month_reads_its_own_days_in_two_queries(self):
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("5"))])
        with self.assertNumQueries(2):
            sheet = month_sheet(self.person, date(2026, 6, 20))
        self.assertTrue(sheet.saved)
        self.assertEqual(sheet.month, JUNE)
        self.assertEqual(sheet.days[1].hours, Decimal("5"))
        self.assertTrue(sheet.days[1].differs)

    def test_a_typical_week_changed_later_never_rewrites_a_saved_month(self):
        save_month(self.person, JUNE, [])
        self.person.tuesday_hours = Decimal("6")
        self.person.save()
        saved, unsaved = month_sheet(self.person, JUNE), month_sheet(self.person, date(2026, 7, 1))
        self.assertEqual(saved.days[1].hours, Decimal("7.5"))
        # The week June was saved with, not today's: Tuesday 2 is as planned.
        self.assertEqual(saved.days[1].typical_hours, Decimal("7.5"))
        self.assertFalse(saved.days[1].differs)
        self.assertEqual(saved.summary.worked_hours, Decimal("151.5"))
        self.assertEqual(unsaved.days[6].hours, Decimal("6"))

    def test_every_saved_month_in_two_queries_newest_first(self):
        for month in (date(2026, 4, 1), date(2026, 6, 1), date(2026, 5, 1)):
            save_month(self.person, month, [])
        other = employee(last_name="Martin", first_name="Paul")
        save_month(other, JUNE, [])
        with self.assertNumQueries(2):
            sheets = saved_month_sheets(self.person)
            labels = [(sheet.label, sheet.summary.worked_hours) for sheet in sheets]
        self.assertEqual([label for label, _hours in labels], ["juin 2026", "mai 2026", "avril 2026"])
        self.assertTrue(all(sheet.saved and sheet.employee == self.person for sheet in sheets))
        self.assertEqual(saved_month_sheets(employee(last_name="Petit")), [])


class TypicalWeekOfASavedMonthTests(TestCase):
    """A saved month keeps the typical week it was first saved with: the
    sheet the employee signed prints its « Semaine type », what it planned
    and the difference from THAT week, and every later correction of the
    month reads it too. The employee's new week reaches the months nobody
    saved yet - and a saved one only through « Revenir à la semaine type »."""

    def setUp(self):
        self.person = employee()
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), kind="conges")])
        # Read now: the sheet holds the employee, whose week changes below.
        self.before = self.read(month_sheet(self.person, JUNE))
        # From now on: Saturday 4 h, 32,5 h a week.
        self.person.saturday_hours = Decimal("4")
        self.person.save()

    def read(self, sheet):
        return (
            sheet.weekly_hours,
            [day.typical_hours for day in sheet.days],
            [day.differs for day in sheet.days],
            [week.typical_total for week in sheet.weeks],
            sheet.summary,
        )

    def test_the_week_is_kept_with_the_month_when_it_is_first_saved(self):
        timesheet = Timesheet.objects.get()
        self.assertEqual(
            timesheet.typical_week,
            (Decimal("0"), Decimal("7.5"), Decimal("6"), Decimal("7.5"), Decimal("7.5"), Decimal("7.5"), Decimal("0")),
        )
        self.assertEqual(timesheet.weekly_hours, Decimal("36"))

    def test_whichever_action_saves_the_month_first(self):
        """October by « Enregistrer », November by « Du … au … », December
        (Christmas on a Friday) by the holidays button."""
        for month, first_save in (
            (date(2026, 10, 1), lambda month: save_month(self.person, month, [])),
            (
                date(2026, 11, 1),
                lambda month: apply_range(self.person, month, date(2026, 11, 3), date(2026, 11, 3), "conges"),
            ),
            (date(2026, 12, 1), lambda month: mark_holidays_off(self.person, month)),
        ):
            with self.subTest(month=month):
                self.assertTrue(first_save(month).created)
                self.assertEqual(Timesheet.objects.get(month=month).weekly_hours, Decimal("32.5"))

    def test_the_saved_month_reads_as_it_did(self):
        after = month_sheet(self.person, JUNE)
        self.assertEqual(self.read(after), self.before)
        self.assertEqual(after.weekly_hours_text, "36")
        # Tuesday 2 on leave differs, as it did; the Saturdays at 7,5 h are
        # what June planned, not a change.
        self.assertEqual([day.date.day for day in after.days if day.differs], [2])

    def test_every_saved_month_is_listed_with_its_own_week(self):
        (june,) = saved_month_sheets(self.person)
        self.assertEqual(self.read(june), self.before)

    def test_a_month_nobody_saved_follows_the_new_week(self):
        july = month_sheet(self.person, date(2026, 7, 1))
        self.assertEqual(july.weekly_hours, Decimal("32.5"))
        self.assertEqual(july.days[3].hours, Decimal("4"))  # Saturday 4 July

    def test_later_corrections_of_the_month_use_its_week(self):
        """Back to « Travail » with no hours posted, or a « Travail » range
        without hours: June's typical Saturday, 7,5 h - not today's 4."""
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 2), kind="travail")])
        apply_range(self.person, JUNE, date(2026, 6, 13), date(2026, 6, 13), "conges")
        save_month(self.person, JUNE, [PostedDay(date(2026, 6, 13), kind="travail")])
        apply_range(self.person, JUNE, date(2026, 6, 20), date(2026, 6, 20), "maladie")
        apply_range(self.person, JUNE, date(2026, 6, 20), date(2026, 6, 20), "travail")
        sheet = month_sheet(self.person, JUNE)
        self.assertEqual([sheet.days[n - 1].hours for n in (2, 13, 20)], [Decimal("7.5")] * 3)
        self.assertFalse(any(day.differs for day in sheet.days))
        self.assertEqual(Timesheet.objects.get().weekly_hours, Decimal("36"))

    def test_back_to_the_typical_week_takes_today_s_and_keeps_it(self):
        outcome = reset_to_typical_week(self.person, JUNE)
        self.assertTrue(outcome.week_changed)
        sheet = month_sheet(self.person, JUNE)
        self.assertEqual(sheet.weekly_hours, Decimal("32.5"))
        self.assertEqual(sheet.days[5].hours, Decimal("4"))  # Saturday 6
        self.assertFalse(any(day.differs for day in sheet.days))
        self.assertEqual(Timesheet.objects.get().saturday_hours, Decimal("4"))
        # Again: the month already is today's week.
        self.assertFalse(reset_to_typical_week(self.person, JUNE).week_changed)
