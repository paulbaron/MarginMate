"""`invoices/rendering.py`: the correction page's inputs drawn in Python.

Django draws an `<input>` through three templates, and the correction page
of an eighty-line invoice draws 870 of them - two thirds of its time. The
renderer writes the same line itself. These tests hold it to the letter of
the templates: every widget below is drawn both ways and compared character
for character, whatever its value - None, empty, markup, a string already
marked safe, a Decimal, a date, an attribute set to True or False - and the
renderer must give way to Django wherever it cannot be sure.

Data invented.
"""

import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django import forms
from django.forms.renderers import get_default_renderer
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from invoices import rendering
from invoices.forms import LineCorrectionForm, LineCorrectionFormSet
from invoices.rendering import PLAIN_INPUTS, PlainInputRenderer, plain_input
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

TRICKY = 'Jus "pomme" & <poire> l\'été'

WIDGETS = [
    forms.TextInput(),
    forms.TextInput(attrs={"placeholder": TRICKY, "maxlength": "255", "autofocus": True, "disabled": False}),
    forms.TextInput(attrs={"data-note": mark_safe("<b>déjà sûr</b>"), "class": "a b"}),
    forms.TextInput(attrs={"placeholder": gettext_lazy("Prix & « quantité »"), "title": mark_safe("a & b")}),
    forms.NumberInput(attrs={"step": "0.01", "placeholder": "HT"}),
    forms.NumberInput(attrs={"min": Decimal("0"), "max": 12, "step": "any"}),
    forms.HiddenInput(),
    forms.HiddenInput(attrs={"data-list": ("a", "b")}),
    forms.CheckboxInput(),
    forms.CheckboxInput(attrs={"class": "js-spread-charge", "required": True}),
    forms.EmailInput(attrs={"autocomplete": "email"}),
    forms.URLInput(),
    forms.PasswordInput(render_value=True),
    forms.DateInput(),
    forms.TimeInput(),
]

VALUES = [
    None,
    "",
    "0",
    "12.50",
    TRICKY,
    mark_safe("<i>sûr</i>"),
    Decimal("-3.1400"),
    Decimal("1E+1"),
    0,
    1234,
    gettext_lazy("Prix & quantité"),
    True,
    False,
    date(2026, 2, 3),
]


def drawn(widget, value, renderer, attrs=None):
    return widget.render("ligne-0-total_ht", value, attrs=attrs, renderer=renderer)


class SameAsTheTemplatesTests(SimpleTestCase):
    def test_every_widget_and_value_is_drawn_as_the_templates_draw_it(self):
        for widget in WIDGETS:
            for value in VALUES:
                for attrs in (None, {"id": "id_ligne-0-total_ht"}, {"id": 'x"<y>', "aria-describedby": "aide"}):
                    with self.subTest(widget=type(widget).__name__, attrs=widget.attrs, value=value, extra=attrs):
                        expected = drawn(widget, value, get_default_renderer(), attrs)
                        self.assertEqual(drawn(widget, value, PlainInputRenderer(), attrs), expected)

    def test_the_plain_inputs_are_really_drawn_in_python(self):
        """Not by falling back to the templates, which would make the test
        above pass for nothing."""
        renderer = PlainInputRenderer()
        with mock.patch.object(type(get_default_renderer()), "render", side_effect=AssertionError("template")):
            for widget in WIDGETS:
                drawn(widget, "1", renderer)

    def test_a_select_is_left_to_django(self):
        widget = forms.Select(choices=[("a", "A & B"), ("b", "<C>")])
        self.assertEqual(drawn(widget, "b", PlainInputRenderer()), drawn(widget, "b", get_default_renderer()))

    def test_an_attribute_the_template_would_read_otherwise_is_left_to_it(self):
        """`widget.attrs.items` reads a key called « items » before the dict's
        own method; a name that is not a string is localised by `{{ name }}`."""
        for attrs in ({"items": "x", "class": "c"}, {1234: "x"}):
            with self.subTest(attrs=attrs):
                context = forms.TextInput(attrs=attrs).get_context("nom", "v", None)
                self.assertIsNone(plain_input(context["widget"]))

    def test_templates_that_no_longer_read_as_expected_are_used_as_they_are(self):
        """An upgrade of Django, or an app overriding input.html, sends every
        widget back to the templates rather than drawing yesterday's line."""
        with mock.patch.object(rendering, "INPUT", rendering.INPUT.replace("<input", "<input data-x")):
            renderer = PlainInputRenderer()
            self.assertFalse(renderer._templates_as_written())
            widget = forms.TextInput(attrs={"placeholder": TRICKY})
            self.assertEqual(drawn(widget, TRICKY, renderer), drawn(widget, TRICKY, get_default_renderer()))
        self.assertTrue(PlainInputRenderer()._templates_as_written())

    def test_a_form_drawn_with_it_reads_its_templates_from_django(self):
        """Labels, errors and the form's own layout are the default
        renderer's."""
        form = LineCorrectionForm(data={"product_name": "", "vat_rate": "abc"})
        form.is_valid()
        default = LineCorrectionForm(data={"product_name": "", "vat_rate": "abc"}, renderer=get_default_renderer())
        default.is_valid()
        self.assertIs(form.renderer, PLAIN_INPUTS)
        self.assertEqual(str(form), str(default))
        self.assertEqual(form.renderer.form_template_name, get_default_renderer().form_template_name)


class CorrectionPageTests(TestCase):
    """The whole page, drawn with the renderer and with Django's: the same
    bytes, CSRF token aside."""

    def setUp(self):
        supplier = make_supplier(name="Grossiste Exemple")
        self.invoice = make_invoice(supplier=supplier)
        for n, (total, rate) in enumerate((("12.40", "0.20"), ("3.10", "0.055"), ("-6.00", "0.20"))):
            make_invoice_line(
                invoice=self.invoice,
                product=make_product(supplier=supplier, raw_name=f'Article "{n}" & <cie>'),
                total_ht=Decimal(total),
                vat_rate=Decimal(rate),
                quantity=-1 if total.startswith("-") else 2,
                total_volume="0.750",
            )

    def page(self):
        html = self.client.get(reverse("invoices:invoice_edit_lines", args=[self.invoice.pk])).content.decode()
        return re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", html)

    def test_the_page_is_the_same_drawn_either_way(self):
        fast = self.page()
        with mock.patch.object(LineCorrectionForm, "default_renderer", None):
            slow = self.page()
        self.assertIn('name="form-0-total_ht"', fast)
        self.assertEqual(fast, slow)

    def test_the_formset_hands_its_forms_the_renderer(self):
        formset = LineCorrectionFormSet(initial=[{"product_name": "x"}])
        self.assertIs(formset.forms[0].renderer, PLAIN_INPUTS)
        self.assertIs(formset.empty_form.renderer, PLAIN_INPUTS)
