"""`{% asset %}` - like `{% static %}`, but the browser notices changes.

Editing marginmate.css or datatable.js and seeing absolutely nothing happen
is a genuinely nasty way to lose half an hour: the page keeps using a cached
copy, so the change looks like it didn't work rather than like it didn't
load. It has already cost that twice here.

Appending the file's modification time makes each edit a new URL, so the
browser fetches it. In production it's equally correct - a deploy changes the
mtime, so nobody is served yesterday's stylesheet against today's markup.

Lives in `inventory` only because a template tag has to live in some app;
nothing about it is inventory-specific.
"""

import os
from decimal import ROUND_HALF_UP, Context, Decimal, getcontext

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.template.defaultfilters import floatformat
from django.templatetags.static import static
from django.utils import formats

from common import group_thousands, plain_number

register = template.Library()


@register.filter
def quantity(value) -> str:
    """A quantity as written: 0.82, 2 - see common.plain_number."""
    try:
        return plain_number(value)
    except (ArithmeticError, ValueError):
        return str(value)


def _floatformat(value, places):
    """`floatformat(value, places)` for a finite Decimal, int or float and an
    int `places` - floatformat's own steps, the same characters, without
    what an amount never asks of it: the « g » / « u » suffixes and
    number_format's general case (no grouping while USE_THOUSAND_SEPARATOR
    is off, its one decimal separator put in directly). A big page prints
    thousands of amounts, and floatformat paid for a language lookup, three
    format lookups and a second pass over every digit each time. None for
    anything else (None, "", a string, NaN, a bool, « "-2" », a number over
    200 digits): floatformat itself answers those.
    tests/test_money_format.py compares the two over every case."""
    kind = type(value)
    if kind is Decimal:
        # floatformat reads Decimal(str(value)): the same Decimal, sign,
        # digits and exponent - a Decimal's string is lossless.
        number = value
    elif kind is int or kind is float:
        number = Decimal(str(value))
    else:
        return None
    if type(places) is not int or not number.is_finite() or settings.USE_THOUSAND_SEPARATOR:
        return None
    _, digits, exponent = number.as_tuple()
    if len(digits) + abs(exponent) > 200:
        return None
    fraction = int(number) - number
    if not fraction and places <= 0:
        return str(int(number))
    shown = abs(places)
    units = len(digits) + (-exponent if fraction else exponent)
    precision = max(getcontext().prec, shown + units + 1)
    rounded = number.quantize(Decimal(1).scaleb(-shown), ROUND_HALF_UP, Context(prec=precision))
    negative, digits, _ = rounded.as_tuple()
    # The exponent is -shown: at least one digit before the point.
    text = "".join(map(str, digits)).rjust(shown + 1, "0")
    sign = "-" if negative and rounded else ""
    if not shown:
        return sign + text
    return sign + text[:-shown] + formats.get_format("DECIMAL_SEPARATOR") + text[-shown:]


@register.filter
def money(value, places=2) -> str:
    """An amount as a page prints it: `floatformat` (same rounding, same
    `places` argument) with its thousands grouped - 16568684.5 is
    « 16 568 684.50 ». The « € » stays in the template. Never for a form
    field's value or a `data-*` figure a script reads: see
    common.group_thousands."""
    text = _floatformat(value, places)
    if text is None:
        text = floatformat(value, places)
    return group_thousands(text)


#: Where each static file was found: a path on disk, never what is in it.
#: Static files are code - the same for every espace - and `finders.find`
#: walks every static folder of every app, several times a page. The date
#: is read again at every call, so an edited file still gets its new `?v=`
#: at once; a file gone from there is looked for again.
_FOUND: dict[str, str] = {}


@receiver(setting_changed)
def _static_folders_changed(*, setting, **kwargs):
    """A test moving the static folders: look again, as Django's own finders
    do (django.test.signals.static_finders_changed)."""
    if setting in {"STATICFILES_DIRS", "STATIC_ROOT", "STATICFILES_FINDERS", "INSTALLED_APPS"}:
        _FOUND.clear()


def _version(path: str) -> int | None:
    """The modification time of the file `path` names, None if none."""
    found = _FOUND.get(path)
    if found is not None:
        try:
            return int(os.path.getmtime(found))
        except OSError:
            _FOUND.pop(path, None)
    absolute = finders.find(path)
    if not absolute:
        return None
    version = int(os.path.getmtime(absolute))
    _FOUND[path] = absolute
    return version


@register.simple_tag
def asset(path: str) -> str:
    url = static(path)
    try:
        version = _version(path)
    except (OSError, ValueError):
        version = None
    if version is None:
        # Missing file, or a storage backend with no path on disk: still give
        # a usable URL rather than breaking the page over a cache hint.
        return url
    return f"{url}?v={version}"
