import html
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.forms.renderers import DjangoTemplates, get_default_renderer
from django.template import Context
from django.template.base import render_value_in_context
from django.template.defaultfilters import stringformat
from django.utils import timezone
from django.utils.choices import BaseChoiceIterator, normalize_choices
from django.utils.functional import cached_property
from django.utils.safestring import SafeData, mark_safe

from common import BlankRowTolerantModelForm
from inventory.models import StockType

from .models import PosProduct, Recipe, RecipeIngredient, RecipeSale, SaleDocument, SaleDocumentLine

# Re-exported: views, menu and « Données » (transfer/sections) import it from
# here; its one definition is beside the till's (recipes/sales.py).
from .sales import MANUAL_SALE_SOURCE
from .services import assert_no_cycle


class SelectWidget(forms.Select):
    """Django's <select>, byte for byte, printed without a template per
    <option>.

    The recipe form draws one ingredient picker per row, each offering every
    article and every recipe - hundreds of options a picker - and Django's
    select.html includes select_option.html, which includes attrs.html, for
    every one of them: most of the page's time. This prints what those three
    templates print from Django's own context (`get_context`: the selection,
    the attributes), each value shown as {{ }} or |stringformat:'s' shows it,
    and leaves anything it does not mirror - another renderer, other
    templates - to them.

    Named so that `BoundField.widget_type` - the class name less « Widget »,
    which _form_fields.html prints as « form-field-select » - is Django's.
    """

    def render(self, name, value, attrs=None, renderer=None):
        if not self._mirrors(renderer):
            return super().render(name, value, attrs, renderer)
        widget = self.get_context(name, value, attrs)["widget"]
        context = Context()
        parts = [f'<select name="{_shown(widget["name"], context)}"{_attributes(widget["attrs"], context)}>']
        for group_name, options, _index in widget["optgroups"]:
            if group_name:
                parts.append(f'\n  <optgroup label="{_shown(group_name, context)}">')
            for option in options:
                parts.append(
                    f'\n  <option value="{_shown_as_s(option["value"], context)}"'
                    f"{_attributes(option['attrs'], context)}>{_shown(option['label'], context)}</option>\n"
                )
            if group_name:
                parts.append("\n  </optgroup>")
        # The renderer strips what the template ends with: the last newline.
        parts.append("\n</select>")
        return mark_safe("".join(parts))

    def _mirrors(self, renderer) -> bool:
        return (
            type(renderer or get_default_renderer()) is DjangoTemplates
            and self.template_name == forms.Select.template_name
            and self.option_template_name == forms.Select.option_template_name
        )


class SelectMultipleWidget(SelectWidget, forms.SelectMultiple):
    """The same, for a <select multiple>."""


def _shown(value, context) -> str:
    """{{ value }} - which, for plain text, is the text escaped."""
    if type(value) is str:
        return html.escape(value)
    return render_value_in_context(value, context)


def _shown_as_s(value, context) -> str:
    """{{ value|stringformat:'s' }} - a safe value stays safe through it."""
    if type(value) is str:
        return html.escape(value)
    text = stringformat(value, "s")
    return _shown(mark_safe(text) if isinstance(value, SafeData) else text, context)


def _attributes(attrs: dict, context) -> str:
    """attrs.html: ` name="value"`, the bare name for True, nothing for False."""
    return "".join(
        f" {_shown(name, context)}" if value is True else f' {_shown(name, context)}="{_shown_as_s(value, context)}"'
        for name, value in attrs.items()
        if value is not False
    )


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


def ingredient_categories() -> list[dict]:
    """[{"name": "Rhums", "sources": ["stock:3", "stock:7"]}, ...]: every
    article category with the articles filed under it, by name - what the
    recipe form's picker offers as « Catégorie : … ». Picking one puts every
    article of it in the row's « OU » group, to be pruned by hand: quicker
    than adding twelve rums one at a time.

    Client-side only. A category is never a form choice - the rows it makes
    each post one article, exactly as rows added one by one do, so nothing
    about saving changes. An article with no category belongs to none."""
    by_name: dict[str, list[str]] = {}
    for stock_type_id, category in (
        StockType.objects.exclude(category="").order_by("category", "name").values_list("id", "category")
    ):
        by_name.setdefault(category, []).append(f"stock:{stock_type_id}")
    return [{"name": name, "sources": sources} for name, sources in by_name.items()]


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
    return [("", "---------"), ("Articles", stock_choices), ("Recettes", recipe_choices)]


class SharedChoices(BaseChoiceIterator):
    """Choices built once and handed to every row as they are.

    A ChoiceField normalises the choices it is given, and its widget does it
    again: the ingredient picker's hundreds of choices were copied twice per
    row. Django hands a choice iterator back untouched, so they are
    normalised here, once.
    """

    def __init__(self, choices):
        self.choices = normalize_choices(choices)

    def __iter__(self):
        return iter(self.choices)


class RecipeForm(forms.ModelForm):
    # What the till sells as this recipe - linked on save (views.
    # _recipe_form_view, through recipes.links), so a recipe written for a till
    # product is linked to it in the same step. Offered: the till products
    # still to link, and this recipe's own.
    pos_products = forms.ModelMultipleChoiceField(
        queryset=PosProduct.objects.none(),
        required=False,
        label="Vendue en caisse sous",
        help_text="Les produits de la caisse dont les ventes sont celles de cette recette. "
        "Un produit lié à une autre recette se détache d'abord depuis « À lier ».",
        widget=SelectMultipleWidget(attrs={"data-pick-list": "", "size": "8"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        own = Q(recipe=self.instance) if self.instance.pk else Q(pk__in=[])
        self.fields["pos_products"].queryset = PosProduct.objects.filter(
            own | Q(recipe__isnull=True, ignored=False)
        ).order_by("-total_quantity", "name")
        self.fields["pos_products"].label_from_instance = lambda product: (
            f"{product.name} ({product.total_quantity} vendus)"
        )
        if self.instance.pk and "pos_products" not in self.initial:
            self.initial["pos_products"] = list(self.instance.pos_products.values_list("pk", flat=True))
        # Neither has to be filled: a preparation is not sold, so it has no
        # price and nothing is sold of it. The model keeps sale_quantity NOT
        # NULL with a default of 1 - « one whole sale » - and clean_ below
        # puts that back, which a field failing « required » first would
        # never reach.
        self.fields["sale_quantity"].required = False

    class Meta:
        model = Recipe
        fields = [
            "name",
            "happy_hour_name",
            "category",
            "yield_quantity",
            "yield_unit",
            "sale_quantity",
            "selling_price_ttc",
            "happy_hour_price_ttc",
            "vat_rate",
        ]
        labels = {
            "name": "Nom",
            "happy_hour_name": "Nom en happy hour sur la caisse",
            "category": "Catégorie",
            "yield_quantity": "Quantité produite",
            "yield_unit": "Unité produite",
            "sale_quantity": "Quantité vendue (dans l'unité produite)",
            "selling_price_ttc": "Prix de vente (TTC)",
            "happy_hour_price_ttc": "Prix happy hour (TTC)",
            "vat_rate": "TVA (ex : 0.20 pour 20%)",
        }
        widgets = {
            "category": forms.TextInput(attrs={"list": "recipe-category-datalist", "autocomplete": "off"}),
        }

    def clean_sale_quantity(self):
        """Blank is one whole sale - what every recipe meant before the
        field existed, and what a cocktail means now."""
        quantity = self.cleaned_data.get("sale_quantity")
        return Decimal("1") if quantity in (None, "") else quantity

    def clean(self):
        """A quantity sold, or a happy-hour price, on a recipe that is not
        sold: two answers that cannot both be true, and kept quietly one of
        them would go on being shown - « Pas vendue directement » above a
        happy-hour margin. The price is what says « pas vendue directement »,
        so it is the one that decides.

        Compared on the CLEANED value, never on what was posted. The box is
        drawn with the stored figure, which Django renders as « 1.0000 »
        from the database - so a guard reading the raw string refused the
        one edit this exists for: opening a preparation filed at 0,00 € and
        clearing its price. Decimal("1.0000") == Decimal("1"), the string
        forms are not.
        """
        cleaned = super().clean()
        # A field that RAISED is dropped from cleaned_data, so « left
        # blank » and « 12,50 » (a comma) looked identical here - and a typo
        # answered « cette recette n'est pas vendue telle quelle » on a
        # quantity box that was perfectly correct.
        if cleaned.get("selling_price_ttc") is not None or "selling_price_ttc" in self.errors:
            return cleaned
        if cleaned.get("sale_quantity") not in (None, Decimal("1")):
            self.add_error(
                "sale_quantity",
                "Sans prix de vente, cette recette n'est pas vendue telle quelle : "
                "laissez la quantité vendue vide, ou donnez-lui un prix.",
            )
        if cleaned.get("happy_hour_price_ttc") is not None:
            self.add_error(
                "happy_hour_price_ttc",
                "Sans prix de vente, cette recette n'est pas vendue telle quelle : "
                "elle ne peut pas avoir de prix en happy hour.",
            )
        return cleaned


class RecipeIngredientForm(BlankRowTolerantModelForm):
    # Exactly one of stock_type/sub_recipe, but presented to the user as a
    # single "pick an ingredient" field - see RecipeIngredient's own
    # docstring for why the model itself keeps them as two FKs.
    source = forms.ChoiceField(label="Ingrédient", widget=SelectWidget)
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
            self._cached_source_choices = SharedChoices(
                ingredient_source_choices(self.form_kwargs.get("parent_recipe"))
            )
        return self._cached_source_choices

    @cached_property
    def empty_form(self):
        """The spare row the page's script copies, built once: the template
        reads four of its fields, and Django's property builds the whole
        form again at every read."""
        return super().empty_form

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
        # RecipeSale.quantity is signed so the till can write a refund back
        # (a pint sold one day and taken back the next nets -1). Nothing
        # types a refund in here, so a minus in this box is a slip, and the
        # guard the model used to give this form for free is kept by hand.
        self.fields["quantity"].validators.append(MinValueValidator(0))

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
        # A line already naming a preparation keeps its own choice, or the
        # document it sits on could not be opened at all.
        self.fields["source"].choices = (
            source_choices
            if source_choices is not None
            else sale_source_choices(keep=self.instance.recipe_id if self.instance.pk else None)
        )
        if self.instance.pk:
            self.initial["source"] = (
                f"recipe:{self.instance.recipe_id}"
                if self.instance.recipe_id
                else f"stock:{self.instance.stock_type_id}"
            )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        source = cleaned.get("source")
        if not source:
            self.add_error("source", "Choisissez une recette ou un article.")
            return cleaned
        kind, _, id_str = source.partition(":")
        if kind == "recipe":
            self.instance.recipe_id = int(id_str)
            self.instance.stock_type_id = None
        else:
            self.instance.stock_type_id = int(id_str)
            self.instance.recipe_id = None
        return cleaned


def sale_source_choices(keep: int | None = None) -> list:
    """Everything sellable: a recipe, or a stock item sold as itself.

    A recipe with no price is a preparation and is NOT sellable - offered, a
    line naming it books its full cost against 0,00 € of revenue
    (`SaleDocumentLine.total_ttc` has nothing to fall back on) while
    `margins.computation` counts the cost, which is the asymmetry CLAUDE.md
    forbids for an article sold as itself: both sides out, or neither. A 0 is
    a price somebody typed, so a comped drink stays offered.

    `keep` puts one back: a line written before its recipe's price was
    cleared must still open, and a choice missing from the list is a form
    that refuses the document rather than a document that can be corrected.
    """
    sellable = Recipe.objects.exclude(selling_price_ttc=None)
    if keep is not None:
        sellable = Recipe.objects.filter(Q(selling_price_ttc__isnull=False) | Q(pk=keep))
    return [
        ("", "---------"),
        ("Recettes", [(f"recipe:{r.pk}", r.name) for r in sellable.order_by("name")]),
        (
            "Articles",
            [(f"stock:{st.pk}", f"{st.name} ({st.get_unit_display()})") for st in StockType.objects.order_by("name")],
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
