"""`|date` is config.template_builtins.date: Django's own filter, answered
without a DateFormat for "d/m/Y" and "Y-m-d". Every date must come out as
Django's filter prints it - directly, and through a template, where an aware
datetime reaches the filter in local time (`expects_localtime`)."""

import zoneinfo
from datetime import UTC, date, datetime, timedelta

from django.template import Context, Engine, Template
from django.template import defaultfilters as django_filters
from django.test import SimpleTestCase, override_settings
from django.utils import timezone, translation

from config import template_builtins

FORMATS = ("d/m/Y", "Y-m-d", "d/m/Y H:i", "j F Y", "DATE_FORMAT", "", None, "d/m/y", "Y-m")
PARIS = zoneinfo.ZoneInfo("Europe/Paris")


def _values():
    days = [date(2026, 1, 1), date(2026, 12, 31), date(2024, 2, 29), date(1, 1, 1), date(999, 9, 9), date(9999, 12, 31)]
    yield from days
    for day in days[:3] + [date(2026, 3, 29), date(2026, 10, 25)]:
        moment = datetime(day.year, day.month, day.day)
        yield moment
        yield moment + timedelta(hours=23, minutes=59, seconds=59)
        yield moment.replace(tzinfo=UTC) + timedelta(hours=23, minutes=30)
        yield moment.replace(tzinfo=PARIS) + timedelta(hours=1, minutes=30)
    # The night the clocks go back, twice the same local hour.
    yield datetime(2026, 10, 25, 2, 30, tzinfo=PARIS, fold=1)
    yield from (None, "", "2026-03-05", "pas une date", 0, 20260305)


def outcome(call, *args):
    """What a call answers, or the kind of error it raises: Django's filter
    refuses a time on a date (« H » on a date), and so must this one."""
    try:
        return call(*args)
    except Exception as error:  # noqa: BLE001 - compared, not handled
        return type(error)


class DateFilterTests(SimpleTestCase):
    def test_the_same_characters_as_django_s_filter(self):
        for language in ("en-us", "fr"):
            for value in _values():
                for fmt in FORMATS:
                    with self.subTest(value=value, format=fmt, language=language), translation.override(language):
                        self.assertEqual(
                            outcome(template_builtins.date, value, fmt), outcome(django_filters.date, value, fmt)
                        )

    def test_every_template_has_it_without_a_load(self):
        node = Template('{{ day|date:"d/m/Y" }}').nodelist[0]
        self.assertIs(node.filter_expression.filters[0][0], template_builtins.date)

    def test_through_a_template_aware_datetimes_in_local_time(self):
        """The same page with Django's filter: an engine without the builtin."""
        sources = (
            '{{ v|date:"d/m/Y" }}',
            "{{ v|date:'Y-m-d' }}",
            '{{ v|date:"d/m/Y H:i" }}',
            '{% load tz %}{% localtime off %}{{ v|date:"d/m/Y" }} {{ v|date:"Y-m-d" }}{% endlocaltime %}',
        )
        django_s = Engine(libraries={"tz": "django.templatetags.tz"})
        django_filter = django_s.from_string(sources[0]).nodelist[0].filter_expression.filters[0][0]
        self.assertIs(django_filter, django_filters.date)
        moments = [*_values(), timezone.now(), datetime(2026, 3, 31, 23, 30, tzinfo=UTC)]
        for source in sources:
            ours, theirs = Template(source), django_s.from_string(source)
            for zone in ("Europe/Paris", "UTC", "Pacific/Kiritimati"):
                with override_settings(TIME_ZONE=zone):
                    for value in moments:
                        with self.subTest(source=source, value=value, zone=zone):
                            self.assertEqual(
                                outcome(ours.render, Context({"v": value})),
                                outcome(theirs.render, Context({"v": value})),
                            )

    def test_an_utc_evening_is_the_next_day_in_paris(self):
        late = datetime(2026, 3, 31, 23, 30, tzinfo=UTC)
        self.assertEqual(Template('{{ v|date:"d/m/Y" }}').render(Context({"v": late})), "01/04/2026")
