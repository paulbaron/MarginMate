"""What the staff tests share. Every name, address and figure here is
INVENTED - the typical week included: the repository is public, and a
timesheet is personal data (an employee's contract week is as much of it as
their name)."""

from decimal import Decimal

from staff.models import Employee

#: Tuesday 7,5 h, Wednesday 6 h, Thursday to Saturday 7,5 h, Sunday and
#: Monday off: 36 h. Two different figures, so a day read with its
#: neighbour's hours shows, and a half hour, so every total goes through the
#: comma.
TYPICAL_WEEK = {
    "tuesday_hours": Decimal("7.5"),
    "wednesday_hours": Decimal("6"),
    "thursday_hours": Decimal("7.5"),
    "friday_hours": Decimal("7.5"),
    "saturday_hours": Decimal("7.5"),
}


def employee(save=True, last_name="Dupont", first_name="Jeanne", **hours):
    """An employee with the typical week above, or with `hours` instead
    (`monday_hours=Decimal("7")`, …) when some are given."""
    person = Employee(last_name=last_name, first_name=first_name, **(hours or TYPICAL_WEEK))
    if save:
        person.save()
    return person


def fields_as_drawn(sheet):
    """The month's form as a page drawing `sheet` posts it: every day's
    three fields, with the values the fields show."""
    data = {}
    for day in sheet.days:
        data[day.hours_field] = day.hours_input
        data[day.kind_field] = day.kind
        data[day.note_field] = day.note
    return data
