"""Django's form renderer, drawing its plain `<input>` widgets in Python.

The correction page (`document_review.html`) draws ten fields a line, and a
wholesaler's invoice of eighty lines is 870 widgets. Django renders each one
through three templates - text.html includes input.html, which includes
attrs.html - and that was two thirds of the page's time, for a line of HTML
whose shape never changes.

`PlainInputRenderer` writes that line itself, character for character what
the templates write: the same escaping (`conditional_escape`'s, as
`{{ … }}` does), the same `stringformat:'s'` of a value, the same tests on
None, True and False. Only where it can be sure of it:

* a widget whose template is one of PLAIN_INPUT_TEMPLATES, each of which
  is nothing but `{% include "django/forms/widgets/input.html" %}`;
* those templates, input.html and attrs.html, read exactly as written
  below - checked against what the template engine actually loads, so an
  upgrade of Django or an override in some app's templates/ sends
  everything back to the templates;
* a type, a name and attribute names that are strings (and no attribute
  called « items », which the template's `widget.attrs.items` would read).

Everything else - a select, a textarea, a label, an error list - goes to
Django's own renderer, untouched. invoices/tests/test_plain_inputs.py
compares the two over every kind of value.
"""

from html import escape

from django.forms.renderers import BaseRenderer, get_default_renderer
from django.utils.safestring import SafeData

#: The widget templates that are input.html and nothing else.
PLAIN_INPUT_TEMPLATES = frozenset(
    f"django/forms/widgets/{name}.html"
    for name in ("text", "number", "hidden", "checkbox", "email", "url", "password", "date", "datetime", "time")
)
INCLUDE_INPUT = '{% include "django/forms/widgets/input.html" %}\n'
INPUT = (
    '<input type="{{ widget.type }}" name="{{ widget.name }}"'
    "{% if widget.value != None %} value=\"{{ widget.value|stringformat:'s' }}\"{% endif %}"
    '{% include "django/forms/widgets/attrs.html" %}>\n'
)
ATTRS = (
    "{% for name, value in widget.attrs.items %}{% if value is not False %} {{ name }}"
    "{% if value is not True %}=\"{{ value|stringformat:'s' }}\"{% endif %}{% endif %}{% endfor %}"
)


def _escaped(text: str) -> str:
    """`{{ text }}` of a string: Django's conditional_escape, whose escape()
    is html.escape - without the wrapper that looks for lazy arguments, a
    third of the time of an input."""
    # ty: hasattr() narrows to `object`; __html__ is SafeData's method, as in conditional_escape.
    return text.__html__() if hasattr(text, "__html__") else escape(text)  # ty: ignore[call-non-callable]


def _as_string(value) -> str:
    """`{{ value|stringformat:'s' }}`: the filter keeps a safe string safe,
    and the variable is escaped unless it is."""
    try:
        text = "%s" % (str(value) if isinstance(value, tuple) else value)
    except (ValueError, TypeError):
        text = ""
    return text if isinstance(value, SafeData) else escape(text)


def _not_none(value) -> bool:
    """`{% if value != None %}`: a comparison that raises is false."""
    try:
        # The template's own comparison, which an object may overload - not
        # `is not None`.
        return value != None
    except Exception:  # noqa: BLE001 - smartif answers False to any error
        return False


def plain_input(widget: dict) -> str | None:
    """The `<input>` input.html draws for this widget context, or None when
    it holds something the templates would print another way (a type, a
    name or an attribute name that is not a string)."""
    kind, name, attrs = widget["type"], widget["name"], widget["attrs"]
    if not (isinstance(kind, str) and isinstance(name, str) and all(isinstance(key, str) for key in attrs)):
        return None
    if "items" in attrs:
        # The template's `widget.attrs.items` would read that key, not the
        # dict's items.
        return None
    html = [f'<input type="{_escaped(kind)}" name="{_escaped(name)}"']
    if _not_none(widget["value"]):
        html.append(f' value="{_as_string(widget["value"])}"')
    for key, value in attrs.items():
        if value is not False:
            html.append(f" {_escaped(key)}")
            if value is not True:
                html.append(f'="{_as_string(value)}"')
    html.append(">")
    return "".join(html)


class PlainInputRenderer(BaseRenderer):
    """The default renderer, plain inputs drawn by `plain_input`.

    One instance for the process (PLAIN_INPUTS): it holds nothing but
    whether the templates read as expected, which is code, not data."""

    def __init__(self):
        self._as_written = None

    @property
    def _default(self):
        return get_default_renderer()

    # What a form, a formset and a field draw with: the default renderer's,
    # whatever FORM_RENDERER says.
    @property
    def form_template_name(self):
        return self._default.form_template_name

    @property
    def formset_template_name(self):
        return self._default.formset_template_name

    @property
    def field_template_name(self):
        return self._default.field_template_name

    @property
    def bound_field_class(self):
        return self._default.bound_field_class

    def get_template(self, template_name):
        return self._default.get_template(template_name)

    def _templates_as_written(self) -> bool:
        if self._as_written is None:
            expected = {name: INCLUDE_INPUT for name in PLAIN_INPUT_TEMPLATES}
            expected["django/forms/widgets/input.html"] = INPUT
            expected["django/forms/widgets/attrs.html"] = ATTRS
            try:
                self._as_written = all(
                    self.get_template(name).template.source == source for name, source in expected.items()
                )
            except Exception:  # noqa: BLE001 - a template that will not load is drawn by Django, which says why
                self._as_written = False
        return self._as_written

    def render(self, template_name, context, request=None):
        if template_name in PLAIN_INPUT_TEMPLATES and self._templates_as_written():
            html = plain_input(context["widget"])
            if html is not None:
                return html
        return self._default.render(template_name, context, request=request)


PLAIN_INPUTS = PlainInputRenderer()
