"""Django's own `date` filter, quicker on the two formats the pages print by
the thousand - and to the character the same.

Every date a page shows is `|date:"d/m/Y"` (CLAUDE.md « Dates are always
|date:"d/m/Y" »), every date a form or a `data-*` reads back
`|date:'Y-m-d'`: a year of invoices is thousands of them on one page.
Django's filter makes a DateFormat for each - a language lookup, a timezone
check, a regular expression over the format - to print three numbers.

A builtin (settings.TEMPLATES' `builtins`, loaded after Django's own), so
every template gets it without a `{% load %}`, as it got Django's. Those
two formats of a date or a datetime only; anything else - another format,
None, "", a string - is Django's filter's. Registered as Django's is
(`expects_localtime`): an aware datetime reaches it in local time.
tests/test_date_filter.py compares the two over every case.
"""

from datetime import date as Date

from django import template
from django.template import defaultfilters

register = template.Library()


@register.filter(expects_localtime=True, is_safe=False)
def date(value, arg=None):
    if isinstance(value, Date):
        # DateFormat's d, m and Y: "%02d" % day, "%02d" % month, "%04d" % year.
        if arg == "d/m/Y":
            return f"{value.day:02d}/{value.month:02d}/{value.year:04d}"
        if arg == "Y-m-d":
            return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    return defaultfilters.date(value, arg)
