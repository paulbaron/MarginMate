"""The establishment, the employees and their typical week, the stored
days - and what the database refuses on its own."""

import importlib
from datetime import date
from decimal import Decimal

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase

from staff.models import ABSENCE_KINDS, WEEKDAY_FIELDS, Employee, Establishment, Timesheet, TimesheetDay
from staff.tests.support import employee


class EstablishmentTests(TestCase):
    def test_one_row_created_on_first_use_and_blank(self):
        first = Establishment.current()
        self.assertEqual((first.pk, first.name, first.address), (1, "", ""))
        first.name = "BAR EXEMPLE"
        first.save()
        self.assertEqual(Establishment.current().name, "BAR EXEMPLE")
        self.assertEqual(Establishment.objects.count(), 1)

    def test_the_address_prints_its_lines_as_typed(self):
        place = Establishment(address="12 rue Imaginaire\r\n\r\n  75000 PARIS  \n")
        self.assertEqual(place.address_lines, ["12 rue Imaginaire", "75000 PARIS"])
        self.assertEqual(Establishment().address_lines, [])


class EmployeeTests(SimpleTestCase):
    def test_the_name_as_the_sheets_print_it(self):
        self.assertEqual(Employee(last_name="Dupont", first_name="Jeanne").display_name, "DUPONT Jeanne")
        self.assertEqual(Employee(last_name=" Lefèvre ", first_name=" Éloïse ").display_name, "LEFÈVRE Éloïse")
        self.assertEqual(Employee(last_name="Dupont", first_name="").display_name, "DUPONT")
        self.assertEqual(str(Employee(last_name="Martin", first_name="Paul")), "MARTIN Paul")

    def test_the_typical_week_monday_first(self):
        person = employee(save=False)
        self.assertEqual(
            person.typical_week,
            (Decimal("0"), Decimal("7.5"), Decimal("6"), Decimal("7.5"), Decimal("7.5"), Decimal("7.5"), Decimal("0")),
        )
        self.assertEqual(person.weekly_hours, Decimal("36"))
        self.assertEqual(person.typical_hours(1), Decimal("7.5"))
        self.assertEqual(person.typical_hours(2), Decimal("6"))
        self.assertEqual(person.typical_hours(6), Decimal("0"))
        self.assertEqual(len(WEEKDAY_FIELDS), 7)

    def test_an_unsaved_employee_given_ints_still_adds_up_in_decimal(self):
        person = Employee(last_name="Dupont", monday_hours=7, tuesday_hours="7.5")
        self.assertEqual(person.weekly_hours, Decimal("14.5"))
        self.assertIsInstance(person.typical_hours(0), Decimal)

    def test_no_eighth_day(self):
        person = employee(save=False)
        for weekday in (-1, 7, "1", None, 1.0):
            with self.subTest(weekday=weekday), self.assertRaises(ValueError):
                person.typical_hours(weekday)

    def test_no_typical_week_is_zero(self):
        self.assertEqual(Employee(last_name="Dupont").weekly_hours, Decimal("0"))


class EmployeeValidationTests(TestCase):
    def test_each_day_from_zero_to_twenty_four(self):
        for hours in (Decimal("0"), Decimal("0.01"), Decimal("7.5"), Decimal("24")):
            with self.subTest(hours=hours):
                Employee(last_name="Dupont", monday_hours=hours).full_clean()

    def test_out_of_a_day_or_past_the_hundredth_is_refused(self):
        for hours in (Decimal("-0.5"), Decimal("24.01"), Decimal("7.555")):
            with self.subTest(hours=hours), self.assertRaises(ValidationError) as caught:
                Employee(last_name="Dupont", sunday_hours=hours).full_clean()
            self.assertIn("sunday_hours", caught.exception.message_dict)

    def test_the_messages_are_french(self):
        with self.assertRaises(ValidationError) as caught:
            Employee(last_name="Dupont", monday_hours=Decimal("-1"), tuesday_hours=Decimal("25")).full_clean()
        self.assertEqual(caught.exception.message_dict["monday_hours"], ["Les heures ne peuvent pas être négatives."])
        self.assertEqual(caught.exception.message_dict["tuesday_hours"], ["Pas plus de 24 h dans une journée."])

    def test_a_last_name_is_needed_a_first_name_is_not(self):
        with self.assertRaises(ValidationError) as caught:
            Employee(last_name="", first_name="Jeanne").full_clean()
        self.assertIn("last_name", caught.exception.message_dict)
        Employee(last_name="Dupont", first_name="").full_clean()

    def test_ordered_by_last_name_then_first_name(self):
        employee(last_name="Martin", first_name="Paul")
        employee(last_name="Dupont", first_name="Luc")
        employee(last_name="Dupont", first_name="Jeanne")
        self.assertEqual(
            [person.display_name for person in Employee.objects.all()], ["DUPONT Jeanne", "DUPONT Luc", "MARTIN Paul"]
        )

    def test_an_employee_with_a_sheet_is_never_deleted(self):
        """A signed timesheet is a record the employer keeps: deactivate."""
        person = employee()
        Timesheet.objects.create(employee=person, month=date(2026, 6, 1))
        with self.assertRaises(ProtectedError):
            person.delete()
        employee(last_name="Martin").delete()


class TimesheetTests(TestCase):
    def setUp(self):
        self.person = employee()

    def test_a_month_is_stored_as_its_first(self):
        with self.assertRaises(ValidationError):
            Timesheet(employee=self.person, month=date(2026, 6, 15)).full_clean()
        with self.assertRaises(ValidationError):
            Timesheet.objects.create(employee=self.person, month=date(2026, 6, 15))
        self.assertFalse(Timesheet.objects.exists())
        Timesheet(employee=self.person, month=date(2026, 6, 1)).full_clean()

    def test_one_sheet_per_employee_and_month(self):
        Timesheet.objects.create(employee=self.person, month=date(2026, 6, 1))
        Timesheet.objects.create(employee=employee(last_name="Martin"), month=date(2026, 6, 1))
        Timesheet.objects.create(employee=self.person, month=date(2026, 7, 1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Timesheet.objects.create(employee=self.person, month=date(2026, 6, 1))

    def test_a_sheet_holds_a_typical_week_of_its_own(self):
        """The week the month was saved with (`staff.timesheet._store` copies
        it): the same seven fields as the employee's, read the same way."""
        sheet = Timesheet(employee=self.person, month=date(2026, 6, 1), **self.person.week_values())
        self.assertEqual(sheet.typical_week, self.person.typical_week)
        self.assertEqual(sheet.weekly_hours, Decimal("36"))
        self.person.saturday_hours = Decimal("4")
        self.assertEqual(sheet.typical_hours(5), Decimal("7.5"))
        self.assertEqual(Timesheet(employee=self.person, month=date(2026, 6, 1)).weekly_hours, Decimal("0"))


class TypicalWeekMigrationTests(TestCase):
    """staff/0002: a sheet saved before a month kept its own week is given
    its employee's week as it stands - the week it was drawn from until
    then, and the only record there is."""

    def test_a_sheet_saved_before_is_given_its_employee_s_week(self):
        migration = importlib.import_module("staff.migrations.0002_timesheet_typical_week")
        dupont = employee()
        martin = employee(last_name="Martin", first_name="Paul", monday_hours=Decimal("7"))
        # As 0001 left them: no week on the sheets.
        june = Timesheet.objects.create(employee=dupont, month=date(2026, 6, 1))
        july = Timesheet.objects.create(employee=martin, month=date(2026, 7, 1))
        migration.copy_the_employees_week(apps, None)
        june.refresh_from_db()
        july.refresh_from_db()
        self.assertEqual(june.typical_week, dupont.typical_week)
        self.assertEqual(july.typical_week, martin.typical_week)
        self.assertEqual((june.weekly_hours, july.weekly_hours), (Decimal("36"), Decimal("7")))


class TimesheetDayTests(TestCase):
    def setUp(self):
        self.sheet = Timesheet.objects.create(employee=employee(), month=date(2026, 6, 1))

    def day(self, **fields):
        return TimesheetDay.objects.create(timesheet=self.sheet, **fields)

    def test_an_absence_is_zero_hours_whatever_it_was_given(self):
        for number, kind in enumerate(sorted(ABSENCE_KINDS), start=1):
            with self.subTest(kind=kind):
                stored = self.day(date=date(2026, 6, number), hours=Decimal("7"), kind=kind)
                stored.refresh_from_db()
                self.assertEqual(stored.hours, Decimal("0"))
                self.assertTrue(stored.is_absence)

    def test_a_rest_day_has_no_hours_either(self):
        stored = self.day(date=date(2026, 6, 7), hours=Decimal("4"), kind=TimesheetDay.Kind.REST)
        stored.refresh_from_db()
        self.assertEqual(stored.hours, Decimal("0"))
        self.assertFalse(stored.is_absence)

    def test_a_worked_day_keeps_its_hours_even_zero(self):
        worked = self.day(date=date(2026, 6, 2), hours=Decimal("7.5"))
        nothing = self.day(date=date(2026, 6, 3), hours=Decimal("0"))
        worked.refresh_from_db()
        nothing.refresh_from_db()
        self.assertEqual((worked.kind, worked.hours), ("travail", Decimal("7.5")))
        self.assertEqual((nothing.kind, nothing.hours), ("travail", Decimal("0")))

    def test_the_database_refuses_hours_on_a_day_not_worked(self):
        """Behind save(): an update() or a bulk write must not get round it."""
        leave = self.day(date=date(2026, 6, 2), kind=TimesheetDay.Kind.PAID_LEAVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TimesheetDay.objects.filter(pk=leave.pk).update(hours=Decimal("7"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TimesheetDay.objects.bulk_create(
                [TimesheetDay(timesheet=self.sheet, date=date(2026, 6, 3), hours=Decimal("7"), kind="maladie")]
            )

    def test_the_database_refuses_hours_out_of_a_day(self):
        worked = self.day(date=date(2026, 6, 2), hours=Decimal("7"))
        for hours in (Decimal("-1"), Decimal("24.5")):
            with self.subTest(hours=hours), self.assertRaises(IntegrityError), transaction.atomic():
                TimesheetDay.objects.filter(pk=worked.pk).update(hours=hours)

    def test_one_row_per_day(self):
        self.day(date=date(2026, 6, 2), hours=Decimal("7"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.day(date=date(2026, 6, 2), hours=Decimal("8"))

    def test_the_days_go_with_their_sheet_in_date_order(self):
        self.day(date=date(2026, 6, 3), hours=Decimal("8"))
        self.day(date=date(2026, 6, 2), hours=Decimal("7"))
        self.assertEqual([day.date.day for day in self.sheet.days.all()], [2, 3])
        self.sheet.delete()
        self.assertFalse(TimesheetDay.objects.exists())

    def test_every_kind_has_a_french_label_and_fits_its_column(self):
        labels = dict(TimesheetDay.Kind.choices)
        self.assertEqual(
            labels,
            {
                "travail": "Travail",
                "repos": "Repos",
                "conges": "Congés payés",
                "repos_comp": "Repos compensateur",
                "ferie": "Férié chômé",
                "maladie": "Arrêt maladie",
                "absence": "Absence",
            },
        )
        width = TimesheetDay._meta.get_field("kind").max_length
        self.assertTrue(all(len(value) <= width for value in labels))
        self.assertEqual(ABSENCE_KINDS, set(labels) - {"travail", "repos"})
