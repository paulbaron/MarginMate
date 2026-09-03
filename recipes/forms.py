from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.forms import BaseInlineFormSet, inlineformset_factory

from common import BlankRowTolerantModelForm
from inventory.models import StockType

from .models import Recipe, RecipeIngredient, RecipeSale, SaleDocument, SaleDocumentLine
from .services import assert_no_cycle

# Sales typed in by hand live under their own source so a till import, which
# only ever rewrites its OWN rows, can never clobber them.
MANUAL_SALE_SOURCE = "manual"


def recipes_usable_as_ingredients(exclude_pk=None):
    """Recipes that can be an ingredient of another recipe: all of them.

    A recipe with alternatives of its own used to be excluded, because "which
    variation's cost do we charge the parent?" had no answer. It does now:
    the option contributes a RANGE, and the parent simply has that many more
    variations of its own (see Recipe.option_variation_count). So "OU"
    between recipes that themselves use "OU" is allowed, and a syrup that can
    be made with sugar or honey multiplies out into every cocktail using it.

    Only the recipe being edited is excluded here - a recipe can't be its own
    ingredient. Longer cycles are caught per row by assert_no_cycle, which
    needs to know which sub-recipe was picked.
    """
    queryset = Recipe.objects.all()
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.order_by("name")


def ingredient_unit_map() -> dict[str, str]:
    """{"stock:<id>": "Litre", "recipe:<id>": "Kilogramme", ...} for every
    selectable ingredient - used client-side (recipe_form.html) to show the
    right unit next to the quantity box the moment an ingredient is picked,
    since it wasn't otherwise obvious which unit a bare number meant."""
    mapping = {f"stock:{st.id}": st.get_unit_display() for st in StockType.objects.all()}
    mapping.update({f"recipe:{r.id}": r.get_yield_unit_display() for r in recipes_usable_as_ingredients()})
    return mapping


def ingredient_source_choices(parent_recipe=None) -> list:
    """The grouped "pick an ingredient" choices, built once per formset
    rather than once per row - it's two full table scans, and a form with
    thirty ingredient rows was doing it thirty times."""
    stock_choices = [
        (f"stock:{st.id}", f"{st.name} ({st.get_unit_display()})") for st in StockType.objects.order_by("name")
    ]
    exclude_pk = parent_recipe.pk if parent_recipe and parent_recipe.pk else None
    recipe_choices = [
        (f"recipe:{r.id}", f"{r.name} ({r.get_yield_unit_display()})")
        for r in recipes_usable_as_ingredients(exclude_pk=exclude_pk)
    ]
    return [("", "---------"), ("Types de stock", stock_choices), ("Recettes", recipe_choices)]


class RecipeForm(forms.ModelForm):
    class Meta:
        model = Recipe
        fields = [
            "name", "happy_hour_name", "category", "yield_quantity", "yield_unit",
            "selling_price_ttc", "happy_hour_price_ttc", "vat_rate",
        ]
        labels = {
            "name": "Nom",
            "happy_hour_name": "Nom en happy hour sur la caisse",
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


class RecipeIngredientForm(BlankRowTolerantModelForm):
    # Exactly one of stock_type/sub_recipe, but presented to the user as a
    # single "pick an ingredient" field - see RecipeIngredient's own
    # docstring for why the model itself keeps them as two FKs.
    source = forms.ChoiceField(label="Ingrédient")
    # Which alternatives-group this row belongs to - assigned by the form's
    # "OU" button (see recipe_form.html), never typed in directly. It's
    # bookkeeping, never something the user types, so it must not on its own
    # make a row look filled in - see BlankRowTolerantFormMixin, without
    # which a row removed in the browser leaves an invisible, unsaveable row
    # behind.
    group = forms.IntegerField(widget=forms.HiddenInput(), required=False, initial=0)

    bookkeeping_fields = ("group",)

    class Meta:
        model = RecipeIngredient
        fields = ["quantity"]
        labels = {"quantity": "Quantité"}

    def __init__(self, *args, parent_recipe=None, source_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent_recipe = parent_recipe
        # The formset builds these once and hands the same list to every row
        # (see BaseRecipeIngredientFormSet); the fallback is for a form used
        # on its own, e.g. in a test.
        self.fields["source"].choices = (
            source_choices if source_choices is not None else ingredient_source_choices(parent_recipe)
        )
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
            self.instance.sub_recipe_id = sub_recipe.pk
            self.instance.stock_type_id = None
            if self.parent_recipe is not None:
                try:
                    assert_no_cycle(self.parent_recipe, sub_recipe)
                except ValidationError as exc:
                    self.add_error("source", exc)
        return cleaned


class BaseRecipeIngredientFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._assign_fresh_groups_to_blank_rows()

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        # Built once here instead of per row - see ingredient_source_choices.
        if "source_choices" not in kwargs:
            kwargs["source_choices"] = self._source_choices()
        return kwargs

    def _source_choices(self):
        if not hasattr(self, "_cached_source_choices"):
            self._cached_source_choices = ingredient_source_choices(self.form_kwargs.get("parent_recipe"))
        return self._cached_source_choices

    def _assign_fresh_groups_to_blank_rows(self):
        """Give each spare blank row its own unused group number.

        `group` defaults to 0, which is not a neutral value - it's a real
        group that an existing ingredient almost always already occupies. So
        filling in the empty row at the bottom of the form quietly made that
        ingredient an ALTERNATIVE to the first one instead of an ingredient
        in its own right, and the recipe reopened saying something the user
        never entered, with nothing on screen to explain it.

        Numbering them here (rather than in the browser) means the value is
        already right in the rendered HTML, so it holds with JavaScript
        disabled and is checkable without one.
        """
        used = set(self.instance.ingredients.values_list("group", flat=True)) if self.instance.pk else set()
        next_group = max(used, default=-1) + 1
        for form in self.forms[self.initial_form_count() :]:
            form.initial["group"] = next_group
            next_group += 1


RecipeIngredientFormSet = inlineformset_factory(
    Recipe,
    RecipeIngredient,
    form=RecipeIngredientForm,
    formset=BaseRecipeIngredientFormSet,
    fk_name="recipe",
    fields=["quantity"],
    extra=1,
    can_delete=True,
)


class ManualSaleForm(forms.ModelForm):
    """A sale typed in by hand, for what the till never saw.

    Saved under its own source so an import can never overwrite it - see
    RecipeSale's uniqueness constraint. Re-entering the same recipe and day
    updates that manual figure rather than erroring, which is what someone
    correcting a number expects.
    """

    class Meta:
        model = RecipeSale
        fields = ["recipe", "sold_on", "quantity"]
        labels = {"recipe": "Recette", "sold_on": "Date", "quantity": "Quantité vendue"}
        widgets = {"sold_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipe"].queryset = Recipe.objects.order_by("name")
        self.fields["sold_on"].initial = timezone.localdate()

    def validate_unique(self):
        """Skipped deliberately: save() upserts.

        RecipeSale is unique per (recipe, day, source), so entering a figure
        for a day that already has one is not an error - it's a correction,
        and the obvious thing to do is update it. Left to ModelForm, that
        same entry comes back as "Recipe sale with this ... already exists",
        which is both alarming and useless when all you did was fix a typo.
        """

    def save(self, commit=True):
        sale, _created = RecipeSale.objects.update_or_create(
            recipe=self.cleaned_data["recipe"],
            sold_on=self.cleaned_data["sold_on"],
            source=MANUAL_SALE_SOURCE,
            defaults={"quantity": self.cleaned_data["quantity"]},
        )
        return sale


class SaleDocumentForm(forms.ModelForm):
    class Meta:
        model = SaleDocument
        fields = ["sold_on", "reference", "note"]
        labels = {"sold_on": "Date de vente", "reference": "Référence", "note": "Note"}
        widgets = {"sold_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            # self.initial, NOT fields["sold_on"].initial: a ModelForm seeds
            # self.initial from the instance, so the key is already there
            # holding None and the field's own initial is never consulted -
            # the date box just renders empty.
            self.initial["sold_on"] = timezone.localdate()


class SaleDocumentLineForm(BlankRowTolerantModelForm):
    """One line: a recipe OR a stock item, chosen from a single field.

    Same single-field-two-FKs shape as RecipeIngredientForm, and for the same
    reason - "what did you sell?" is one question, not two.
    """

    source = forms.ChoiceField(label="Vendu")

    bookkeeping_fields = ()

    class Meta:
        model = SaleDocumentLine
        fields = ["quantity", "unit_price_ttc"]
        labels = {"quantity": "Quantité", "unit_price_ttc": "Prix unitaire TTC"}

    def __init__(self, *args, source_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit_price_ttc"].required = False
        self.fields["source"].choices = (
            source_choices if source_choices is not None else sale_source_choices()
        )
        if self.instance.pk:
            self.initial["source"] = (
                f"recipe:{self.instance.recipe_id}" if self.instance.recipe_id
                else f"stock:{self.instance.stock_type_id}"
            )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        source = cleaned.get("source")
        if not source:
            self.add_error("source", "Choisissez une recette ou un type de stock.")
            return cleaned
        kind, _, id_str = source.partition(":")
        if kind == "recipe":
            self.instance.recipe_id = int(id_str)
            self.instance.stock_type_id = None
        else:
            self.instance.stock_type_id = int(id_str)
            self.instance.recipe_id = None
        return cleaned


def sale_source_choices() -> list:
    """Everything sellable: a recipe, or a stock item sold as itself."""
    return [
        ("", "---------"),
        ("Recettes", [(f"recipe:{r.pk}", r.name) for r in Recipe.objects.order_by("name")]),
        (
            "Types de stock",
            [
                (f"stock:{st.pk}", f"{st.name} ({st.get_unit_display()})")
                for st in StockType.objects.order_by("name")
            ],
        ),
    ]


class BaseSaleDocumentLineFormSet(BaseInlineFormSet):
    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        # Built once rather than per row - it's two full table scans.
        if "source_choices" not in kwargs:
            if not hasattr(self, "_cached_choices"):
                self._cached_choices = sale_source_choices()
            kwargs["source_choices"] = self._cached_choices
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(f.cleaned_data and not f.cleaned_data.get("DELETE") for f in self.forms):
            raise forms.ValidationError("Ajoutez au moins une ligne.")


SaleDocumentLineFormSet = inlineformset_factory(
    SaleDocument,
    SaleDocumentLine,
    form=SaleDocumentLineForm,
    formset=BaseSaleDocumentLineFormSet,
    fields=["quantity", "unit_price_ttc"],
    extra=3,
    can_delete=True,
)
