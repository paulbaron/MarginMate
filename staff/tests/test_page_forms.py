"""staff/tests/page_forms.py submits a page's forms as a browser does: what a
test posts is what the owner's click would send, or the test tests a request
nobody makes. Pinned here for what a browser does not send - a disabled
control, and since « Format du relevé » hides a CSV's fields for another
kind of file, every control of a `<fieldset disabled>` (but its first
`<legend>`, as the HTML standard says)."""

from django.test import SimpleTestCase

from staff.tests.page_forms import forms_of

PAGE = """
<form method="post" action="/essai/">
    <input name="tete" value="1">
    <input name="eteint" value="2" disabled>
    <fieldset disabled>
        <legend>Titre <input name="dans_la_legende" value="3"></legend>
        <input name="cache" value="4">
        <select name="choix"><option value="a" selected>A</option></select>
        <fieldset>
            <input name="plus_bas" value="5">
        </fieldset>
        <legend><input name="seconde_legende" value="6"></legend>
    </fieldset>
    <fieldset>
        <input name="ouvert" value="7">
    </fieldset>
    <input name="apres" value="8">
    <button type="submit" name="action" value="ok">OK</button>
</form>
"""


class DisabledFieldsetTests(SimpleTestCase):
    def test_a_disabled_fieldset_sends_nothing_but_its_first_legend(self):
        (form,) = forms_of(PAGE)
        self.assertEqual(
            form.submission(),
            [("tete", "1"), ("dans_la_legende", "3"), ("ouvert", "7"), ("apres", "8"), ("action", "ok")],
        )
        for name in ("eteint", "cache", "choix", "plus_bas", "seconde_legende"):
            with self.subTest(control=name):
                self.assertTrue(form.control(name).disabled)

    def test_a_fieldset_not_disabled_disables_nothing(self):
        (form,) = forms_of(PAGE.replace("<fieldset disabled>", "<fieldset>"))
        sent = [name for name, _value in form.submission()]
        self.assertEqual(
            sent,
            ["tete", "dans_la_legende", "cache", "choix", "plus_bas", "seconde_legende", "ouvert", "apres", "action"],
        )
