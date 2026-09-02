from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from inventory.models import StockType

from .models import Recipe, RecipeIngredient
from .services import assert_no_cycle


def ingredient_unit_map() -> dict[str, str]:
    """{"stock:<id>": "Litre", "recipe:<id>": "Kilogramme", ...} for every
    selectable ingredient - used client-side (recipe_form.html) to show the
    right unit next to the quantity box the moment an ingredient is picked,
    since it wasn't otherwise obvious which unit a bare number meant."""
    mapping = {f"stock:{st.id}": st.get_unit_display() for st in StockType.objects.all()}
    mapping.update(
        {f"recipe:{r.id}": r.get_yield_unit_display() for r in Recipe.objects.all() if not r.has_variations}
    )
    return mapping


class RecipeForm(forms.ModelForm):
    class Meta:
        model = Recipe
        fields = ["name", "category", "yield_quantity", "yield_unit", "selling_price_ttc", "happy_hour_price_ttc", "vat_rate"]
        labels = {
            "name": "Nom",
            "category": "Catégorie",
            "yield_quantity": "Quantité produite",
            "yield_unit": "Unité produite",
            "selling_price_ttc": "Prix de vente (TTC)",
            "happy_hour_price_ttc": "Prix happy hour (TTC)",
            "vat_rate": "TVA (ex : 0.20 pour 20%)",
        }
        widgets = {
            "category": forms.TextInput(attrs={"list": "recipe-category-datalist", "autocomplete": "off"}),
        }


class RecipeIngredientForm(forms.ModelForm):
    # Exactly one of stock_type/sub_recipe, but presented to the user as a
    # single "pick an ingredient" field - see RecipeIngredient's own
    # docstring for why the model itself keeps them as two FKs.
    source = forms.ChoiceField(label="Ingrédient")
    # Which alternatives-group this row belongs to - assigned by the form's
    # "OU" button (see recipe_form.html), never typed in directly.
    group = forms.IntegerField(widget=forms.HiddenInput(), required=False, initial=0)

    class Meta:
        model = RecipeIngredient
        fields = ["quantity"]
        labels = {"quantity": "Quantité"}

    def __init__(self, *args, parent_recipe=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent_recipe = parent_recipe
        stock_choices = [(f"stock:{st.id}", f"{st.name} ({st.get_unit_display()})") for st in StockType.objects.order_by("name")]
        recipe_qs = Recipe.objects.order_by("name")
        if parent_recipe and parent_recipe.pk:
            recipe_qs = recipe_qs.exclude(pk=parent_recipe.pk)
        # A recipe with variations of its own can't be costed as a single
        # ingredient (which cost would it even use?) - see Recipe.cost_ht's
        # own docstring on why that's only ever called on a single-variation
        # recipe. Excluded from the choices entirely rather than allowed and
        # rejected on save, so there's nothing confusing to pick from.
        recipe_choices = [
            (f"recipe:{r.id}", f"{r.name} ({r.get_yield_unit_display()})") for r in recipe_qs if not r.has_variations
        ]
        self.fields["source"].choices = [
            ("", "---------"),
            ("Types de stock", stock_choices),
            ("Recettes", recipe_choices),
        ]
        if self.instance.pk:
            self.initial["group"] = self.instance.group
            if self.instance.stock_type_id:
                self.initial["source"] = f"stock:{self.instance.stock_type_id}"
            elif self.instance.sub_recipe_id:
                self.initial["source"] = f"recipe:{self.instance.sub_recipe_id}"

    def clean(self):
        cleaned = super().clean()
        if self.cleaned_data.get("DELETE"):
            return cleaned
        source = cleaned.get("source")
        if not source:
            self.add_error("source", "Choisissez un ingrédient.")
            return cleaned

        self.instance.group = cleaned.get("group") or 0
        kind, _, id_str = source.partition(":")
        if kind == "stock":
            self.instance.stock_type_id = int(id_str)
            self.instance.sub_recipe_id = None
        elif kind == "recipe":
            sub_recipe = Recipe.objects.filter(pk=int(id_str)).first()
            if sub_recipe is None:
                self.add_error("source", "Cette recette n'existe plus.")
                return cleaned
            if sub_recipe.has_variations:
                self.add_error("source", f'"{sub_recipe.name}" a plusieurs variations et ne peut pas être utilisée comme ingrédient.')
                return cleaned
            self.instance.sub_recipe_id = sub_recipe.pk
            self.instance.stock_type_id = None
            if self.parent_recipe is not None:
                try:
                    assert_no_cycle(self.parent_recipe, sub_recipe)
                except ValidationError as exc:
                    self.add_error("source", exc)
        return cleaned


RecipeIngredientFormSet = inlineformset_factory(
    Recipe,
    RecipeIngredient,
    form=RecipeIngredientForm,
    fk_name="recipe",
    fields=["quantity"],
    extra=1,
    can_delete=True,
)
