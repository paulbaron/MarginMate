"""A saved month keeps the typical week it was saved with (`Timesheet` is a
`TypicalWeek` too - see staff/models.py).

A second migration rather than an edit of 0001: whichever database already
created the staff tables keeps working. A sheet saved before this migration
never recorded its week, so it is given its employee's week as it stands -
the only record there is, and the one those sheets were drawn from until
now."""

from decimal import Decimal

import django.core.validators
from django.db import migrations, models

WEEKDAY_FIELDS = (
    ("monday_hours", "lundi"),
    ("tuesday_hours", "mardi"),
    ("wednesday_hours", "mercredi"),
    ("thursday_hours", "jeudi"),
    ("friday_hours", "vendredi"),
    ("saturday_hours", "samedi"),
    ("sunday_hours", "dimanche"),
)


def _hours_field(verbose_name):
    return models.DecimalField(
        decimal_places=2,
        default=Decimal("0"),
        max_digits=4,
        validators=[
            django.core.validators.MinValueValidator(Decimal("0"), message="Les heures ne peuvent pas être négatives."),
            django.core.validators.MaxValueValidator(Decimal("24"), message="Pas plus de 24 h dans une journée."),
        ],
        verbose_name=verbose_name,
    )


def copy_the_employees_week(apps, schema_editor):
    Timesheet = apps.get_model("staff", "Timesheet")
    names = [name for name, _label in WEEKDAY_FIELDS]
    sheets = list(Timesheet.objects.select_related("employee"))
    for sheet in sheets:
        for name in names:
            setattr(sheet, name, getattr(sheet.employee, name))
    Timesheet.objects.bulk_update(sheets, names)


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0001_initial"),
    ]

    operations = [
        *(
            migrations.AddField(model_name="timesheet", name=name, field=_hours_field(label))
            for name, label in WEEKDAY_FIELDS
        ),
        migrations.RunPython(copy_the_employees_week, migrations.RunPython.noop),
    ]
