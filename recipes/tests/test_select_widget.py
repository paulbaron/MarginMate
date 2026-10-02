"""The recipe form's pickers, printed without a template per <option> - and
to the byte what Django's templates printed.

* `SelectWidget` and `SelectMultipleWidget` render what `forms.Select` and
  `forms.SelectMultiple` render, over every shape of choice the forms hand
  them (groups, a value outside them, duplicates, numbers, None, safe and
  lazy labels, markup in a name) and of attribute (True, False, None, 0,
  an empty string);
* anything they do not mirror goes to Django's own rendering;
* the whole recipe form - to edit, to create, refused - is the page the stock
  widgets drew;
* the choices are built once and shared by every row, and still validate.

Every name invented.
"""

import re
from decimal import Decimal
from unittest import mock

from django import forms
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from recipes.forms import RecipeIngredientFormSet, SelectMultipleWidget, SelectWidget
from recipes.models import PosProduct, RecipeIngredient
from tests.factories import make_ingredient, make_priced_stock_type, make_recipe

CHOICES = [
    ("", "---------"),
    ("Articles", [("stock:1", 'Rhum <ambré> & "vieux" (L)'), ("stock:2", "Citron vert (kg)"), ("stock:3", "L'eau")]),
    ("Recettes", [("recipe:4", "Sirop d'agave (L)")]),
    ("", [("loose:1", "Dans un groupe sans nom")]),
    ("loose:2", "Hors groupe"),
]

ODD_CHOICES = [
    (None, "Aucun"),
    (1, "Un"),
    (2, 2),
    ("a", "Premier a"),
    ("a", "Second a"),
    ("safe", mark_safe("<b>déjà sûr</b>")),
    ("lazy", gettext_lazy("Paresseux")),
    (mark_safe("v&1"), "Valeur sûre"),
    (Decimal("1.50"), "Un et demi"),
]

ATTRS = [
    None,
    {"id": "id_ingredients-0-source"},
    {"id": "x", "required": True, "disabled": False, "data-none": None, "data-zero": 0, "data-empty": ""},
    {"class": 'a"b<c>', "data-pick-list": "", "size": "8"},
]


class SelectWidgetTests(SimpleTestCase):
    def assertSameAsDjango(self, ours, django, choices, values):
        for attrs in ATTRS:
            for value in values:
                with self.subTest(attrs=attrs, value=value):
                    self.assertEqual(
                        ours(attrs=attrs, choices=choices).render("ingredients-0-source", value, attrs={"id": "y"}),
                        django(attrs=attrs, choices=choices).render("ingredients-0-source", value, attrs={"id": "y"}),
                    )
                    self.assertEqual(
                        ours(attrs=attrs, choices=choices).render("n", value),
                        django(attrs=attrs, choices=choices).render("n", value),
                    )

    def test_a_select(self):
        self.assertSameAsDjango(
            SelectWidget, forms.Select, CHOICES, [None, "", "stock:2", "recipe:4", "loose:1", "loose:2", "absent"]
        )

    def test_odd_choices(self):
        self.assertSameAsDjango(SelectWidget, forms.Select, ODD_CHOICES, [None, "", 1, "2", "a", "safe", "v&1", "1.50"])

    def test_a_select_multiple(self):
        values = [None, [], ["stock:1", "recipe:4"], ["loose:1", "loose:2", "absent"], "stock:3"]
        self.assertSameAsDjango(SelectMultipleWidget, forms.SelectMultiple, CHOICES, values)
        self.assertSameAsDjango(SelectMultipleWidget, forms.SelectMultiple, ODD_CHOICES, [["a", 1], [None]])

    def test_no_choices(self):
        self.assertSameAsDjango(SelectWidget, forms.Select, [], [None, "x"])

    def test_what_it_does_not_mirror_is_djangos(self):
        class OtherOptions(SelectWidget):
            option_template_name = "elsewhere/option.html"

        self.assertTrue(SelectWidget()._mirrors(None))
        self.assertFalse(OtherOptions()._mirrors(None))
        self.assertFalse(SelectWidget()._mirrors(object()))
        with mock.patch.object(forms.Select, "render", return_value="django's") as django_render:
            self.assertEqual(SelectWidget().render("n", None, renderer=object()), "django's")
        django_render.assert_called_once()

    def test_the_field_keeps_its_css_class(self):
        """_form_fields.html prints « form-field-{{ field.widget_type }} »,
        which Django derives from the widget's class name."""

        class Picked(forms.Form):
            one = forms.ChoiceField(choices=CHOICES, widget=SelectWidget)
            many = forms.MultipleChoiceField(choices=CHOICES, widget=SelectMultipleWidget)

        form = Picked()
        self.assertEqual((form["one"].widget_type, form["many"].widget_type), ("select", "selectmultiple"))


def without_tokens(content: bytes) -> str:
    return re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", content.decode())


class RecipeFormPageTests(TestCase):
    """The pages, drawn by these widgets and by Django's own, side by side."""

    def setUp(self):
        self.syrup = make_recipe(name='Sirop "maison" <menthe>', selling_price_ttc=None)
        make_ingredient(self.syrup, stock_type=make_priced_stock_type(name="Sucre & co"), quantity="0.5")
        self.recipe = make_recipe(name="Mojito")
        make_ingredient(self.recipe, stock_type=make_priced_stock_type(name="Rhum blanc"), quantity="0.05", group=0)
        make_ingredient(self.recipe, stock_type=make_priced_stock_type(name="Rhum ambré"), quantity="0.05", group=0)
        make_ingredient(self.recipe, sub_recipe=self.syrup, quantity="0.02", group=1)
        PosProduct.objects.create(name="Mojito <HH>", recipe=self.recipe, total_quantity=3)
        self.pending = PosProduct.objects.create(name="Mojito & co", total_quantity=9)

    def assertSamePage(self, get):
        ours = without_tokens(get().content)
        with mock.patch.object(SelectWidget, "_mirrors", return_value=False):
            django = without_tokens(get().content)
        self.assertIn("<optgroup", ours)
        self.assertEqual(ours, django)

    def test_editing_a_recipe(self):
        self.assertSamePage(lambda: self.client.get(reverse("recipes:recipe_update", args=[self.recipe.pk])))

    def test_a_new_recipe_for_a_till_product(self):
        self.assertSamePage(lambda: self.client.get(reverse("recipes:recipe_create") + f"?caisse={self.pending.pk}"))

    def test_a_refused_form_drawn_again(self):
        ingredient = self.recipe.ingredients.order_by("id").first()
        data = {
            "name": "",
            "yield_quantity": "1",
            "yield_unit": "UNIT",
            "selling_price_ttc": "9",
            "vat_rate": "0.20",
            "pos_products": [str(self.pending.pk)],
            "ingredients-TOTAL_FORMS": "2",
            "ingredients-INITIAL_FORMS": "1",
            "ingredients-MIN_NUM_FORMS": "0",
            "ingredients-MAX_NUM_FORMS": "1000",
            "ingredients-0-id": str(ingredient.pk),
            "ingredients-0-source": f"recipe:{self.syrup.pk}",
            "ingredients-0-quantity": "0.03",
            "ingredients-0-group": "0",
            "ingredients-1-source": "stock:999999",
            "ingredients-1-quantity": "1",
            "ingredients-1-group": "5",
        }
        url = reverse("recipes:recipe_update", args=[self.recipe.pk])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<option value="recipe:{self.syrup.pk}" selected>')
        self.assertSamePage(lambda: self.client.post(url, data))


class SharedChoicesTests(TestCase):
    def setUp(self):
        self.recipe = make_recipe(name="Moscow Mule")
        self.vodka = make_priced_stock_type(name="Vodka")
        make_ingredient(self.recipe, stock_type=self.vodka)
        make_ingredient(self.recipe, stock_type=make_priced_stock_type(name="Ginger beer"), group=1)

    def test_every_row_shares_one_list(self):
        formset = RecipeIngredientFormSet(instance=self.recipe, form_kwargs={"parent_recipe": self.recipe})
        first, second, spare = (form.fields["source"] for form in formset.forms)
        self.assertIs(first.choices, second.choices)
        self.assertIs(first.choices, spare.choices)
        self.assertIs(first.choices, formset.empty_form.fields["source"].choices)
        self.assertIs(formset.empty_form, formset.empty_form)
        articles = dict(first.choices)["Articles"]
        self.assertIn(f"stock:{self.vodka.pk}", [value for value, _label in articles])

    def test_a_choice_is_still_checked(self):
        def posted(source):
            data = {
                "ingredients-TOTAL_FORMS": "1",
                "ingredients-INITIAL_FORMS": "0",
                "ingredients-MIN_NUM_FORMS": "0",
                "ingredients-MAX_NUM_FORMS": "1000",
                "ingredients-0-source": source,
                "ingredients-0-quantity": "0.04",
                "ingredients-0-group": "3",
            }
            return RecipeIngredientFormSet(data, instance=self.recipe, form_kwargs={"parent_recipe": self.recipe})

        self.assertTrue(posted(f"stock:{self.vodka.pk}").is_valid())
        refused = posted("stock:999999")
        self.assertFalse(refused.is_valid())
        self.assertIn("source", refused.forms[0].errors)
        self.assertEqual(RecipeIngredient.objects.filter(recipe=self.recipe).count(), 2)
