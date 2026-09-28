"""`{{ value|hours }}` - hours the one way the pages and the PDF write them
(`timesheet.format_hours`): « 7 », « 7,5 », « 0 ». Django's own
`floatformat` would print « 7.50 » - a dot, and a float on the way."""

from django import template

from ..timesheet import format_hours

register = template.Library()


@register.filter
def hours(value) -> str:
    try:
        return format_hours(value)
    except (ArithmeticError, ValueError):
        return str(value)
