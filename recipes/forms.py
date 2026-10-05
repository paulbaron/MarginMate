import html
from dataclasses import dataclass, field
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import Q
from django.forms import BaseInlineFormSet, BoundField, inlineformset_factory
from django.forms.renderers import DjangoTemplates, get_default_renderer
from django.template import Context
from django.template.base import render_value_in_context
from django.template.defaultfilters import stringformat
from django.utils import timezone
from django.utils.choices import BaseChoiceIterator, normalize_choices
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import SafeData, mark_safe

from common import AMBIGUOUS_THOUSANDS, BlankRowTolerantModelForm, file_too_big, plain_number, read_amount
from inventory.models import StockType
from invoices.forms import EARLIEST_DOCUMENT_DATE, NUL_REFUSED, check_document_date

from .models import (
    LINE_CONSUMED_UNTIED,
    PosProduct,
    Recipe,
    RecipeIngredient,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
from .sale_einvoice import MAX_SALE_LINES
from .sale_files import EXTENSION_REFUSED, SALE_FILE_ACCEPT, SALE_FILE_EXTENSIONS, file_extension
from .sale_lines import consumption_doubt, tieable
from .services import assert_no_cycle

# Sales typed in by hand live under their own source so a till import, which
# only ever rewrites its OWN rows, can never clobber them.
MANUAL_SALE_SOURCE = "manual"


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
    # behind. Bounded because the model's own validators never see it (it is
    # not in Meta.fields): a tampered or stale form posting -1 or 10**25 is
    # the form again with a message, not a 500 from the save. The top is
    # SQLite's, not a smaller cap: an archive may carry large group numbers.
    group = forms.IntegerField(
        widget=forms.HiddenInput(),
        required=False,
        initial=0,
        min_value=0,
        max_value=2**63 - 1,
        error_messages=dict.fromkeys(
            ("invalid", "min_value", "max_value"),
            "Cette ligne n'a pas pu être lue : rechargez la page, puis saisissez-la de nouveau.",
        ),
    )

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


# -- « Factures de vente » (recipes/sale_files.py, sale_document_form.html) ----------------------------------------------

#: What « Compte » means, choice by choice - under each of its radio buttons,
#: on the document's page and on the read card (spec §2.5, §5.3).
COUNTING_HELP: dict[str, str] = {
    SaleDocument.Counting.COUNTED: (
        "Une vente hors caisse : ses lignes reliées sortent du stock, son montant compte dans les marges."
    ),
    SaleDocument.Counting.TILL: (
        "Elle documente des ventes déjà tapées en caisse (une note réglée par virement) : ni les marges ni le stock "
        "ne la comptent. Son virement se compte sur « Entrées d'argent » comme la caisse l'a pris : choisissez "
        "« Avoir » sur l'entrée si la caisse l'a noté en avoir."
    ),
    SaleDocument.Counting.DEPOSIT: (
        "Une facture d'acompte : la facture finale comptera la vente ; celle-ci ne compte ni dans les marges ni dans "
        "le stock."
    ),
}

#: Every refusal of a sale document's page, in French (LANGUAGE_CODE is
#: en-us: Django's own would be English).
UNKNOWN_CHOICE = "Choix inconnu : rechargez la page."
DATE_REQUIRED = "Saisissez la date de la vente."
DATE_UNREADABLE = "Date illisible : JJ/MM/AAAA."
TOO_LONG = "{limit} caractères au plus."
NUMBER_TAKEN = "Une facture de vente porte déjà le n° {number} (du {day}) : ouvrez-la plutôt."
NOTHING_SOLD = "Ajoutez au moins une ligne, ou le total de la facture."
TOTAL_UNREADABLE = "{label} illisible : tapez un montant comme 1 250,00 (2 décimales au plus)."
TOTAL_AMBIGUOUS = "{label} ambigu : tapez 1 500 ou 1,50."
HT_WITHOUT_TTC = "Total HT : tapez aussi le total TTC de la facture."
HT_ABOVE_TTC = "Total HT : du même signe que le total TTC, et pas plus grand."
FILE_AGAIN = "Choisissez le fichier à nouveau : un navigateur ne le garde pas."
FILE_MISSING = "Aucun fichier reçu : rechargez la page, puis choisissez-le à nouveau."
FILE_EMPTY = "Le fichier envoyé est vide."
LINE_GONE = "Cette ligne n'existe plus : rechargez la page."
LINES_FORM_BROKEN = "Les lignes n'ont pas pu être lues : rechargez la page."
TOO_MANY_LINES_TYPED = f"{MAX_SALE_LINES} lignes au plus : ajoutez le reste au total de la facture."
LABEL_TOO_LONG = "Libellé : 255 caractères au plus."
QUANTITY_REQUIRED = "Quantité : tapez un nombre."
QUANTITY_UNREADABLE = "Quantité illisible : un nombre, 4 décimales au plus."
PRICE_UNREADABLE = "Prix illisible : tapez un montant comme 12,50 (2 décimales au plus)."
PRICE_AMBIGUOUS = "Prix ambigu : tapez 1 500 ou 1,50."
FREE_LINE_PRICE = "Prix unitaire TTC : une ligne sans recette ni article a besoin de son prix."
ARTICLE_RATE_PRICE = "Prix unitaire TTC : un article avec un taux de TVA a besoin de son prix."
NEGATIVE_PRICE = "Prix unitaire TTC : un prix n'est pas négatif ; un avoir se tape en quantité négative."
RATE_REFUSED = "TVA : un taux entre 0 et 100 %."
CONSUMED_UNREADABLE = "Quantité consommée illisible : un nombre, 4 décimales au plus."
CONSUMED_SIGN_TYPED = "Quantité consommée : du même signe que la quantité."
CONSUMED_SIGN_TIE = "Quantité consommée : du même signe que la ligne (un avoir rend du stock, une vente en prend)."
CONSUMED_ZERO = "0 : rien n'est sorti du stock, la ligne compte sans coût."
CONSUMED_CLEARED = "Quantité consommée remise à la quantité facturée (nouvelle correspondance) : ligne « {label} »."
TIE_UNKNOWN = "Correspond à : choix inconnu, rechargez la page."
REBUILT_NOT_TIED = "Correspond à : une ligne reconstituée ne se relie pas."
MIRROR_NOT_TIED = "Correspond à : une ligne dont la quantité et le montant ne sont pas du même signe ne se relie pas."
#: What the grid says in place of the select of a line never tied.
REBUILT_LINE = "reconstituée depuis sa table de TVA : rien à relier"
MIRROR_LINE = (
    "la quantité et le montant ne sont pas du même signe (voyez « Contrôles de la facture ») : elle ne se relie pas"
)
#: The blank choice of what a line sold: a line tied to nothing.
NO_SOURCE = "— rien (ligne sans recette ni article)"
#: The <datalist> of earlier customers the « Client » field offers.
KNOWN_CUSTOMERS_LIST = "known-customers"

HUNDRED = Decimal("100")
RATE_PLACES = Decimal("0.0001")
#: « TVA % » as a page draws it: two decimals.
CENTS_PLACES = Decimal("0.01")


def _text_errors(limit: int) -> dict:
    return {"max_length": TOO_LONG.format(limit=limit), "null_characters_not_allowed": NUL_REFUSED}


class TypedAmountField(forms.Field):
    """A figure typed as a person types it - « 12,50 », « 1 250 »
    (common.read_amount) - exact to `places` decimals, within a
    DecimalField(`digits`, `places`): a Decimal, None for nothing typed;
    anything else `unreadable` (a NUL included: no figure holds one). With
    `ambiguous`, « 1,500 » - one separator, three digits after it - is asked
    again rather than read as 1,50 (common.AMBIGUOUS_THOUSANDS).

    Compared by its VALUE (`has_changed`): drawn « 2.0000 » from the
    database and posted back « 2 », it has not changed - which is what « a
    tie changed with the consumed quantity left as drawn » reads."""

    widget = forms.TextInput

    def __init__(self, *, places: int, digits: int, unreadable: str, ambiguous: str = "", **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)
        self.places, self.digits = places, digits
        self.error_messages.update(unreadable=unreadable, ambiguous=ambiguous)

    def to_python(self, value) -> Decimal | None:
        text = "" if value in self.empty_values else str(value).strip()
        if not text:
            return None
        amount = read_amount(text, self.places, digits=self.digits)
        if amount is None:
            raise ValidationError(self.error_messages["unreadable"], code="unreadable")
        if self.error_messages["ambiguous"] and AMBIGUOUS_THOUSANDS.fullmatch("".join(text.split())):
            raise ValidationError(self.error_messages["ambiguous"], code="ambiguous")
        return amount

    def prepare_value(self, value):
        """A stored figure drawn as a person writes it: an amount with its
        cents (« 12.50 »), a quantity plainly (« 0.7 », « 2 »)."""
        if isinstance(value, Decimal):
            return format(value, "f") if self.places <= 2 else plain_number(value)
        return value


def _total_field(label: str, short: str, help_text: str) -> TypedAmountField:
    """A document's total typed: signed (a credit note), to the cent."""
    return TypedAmountField(
        label=label,
        help_text=help_text,
        places=2,
        digits=12,
        unreadable=TOTAL_UNREADABLE.format(label=short),
        ambiguous=TOTAL_AMBIGUOUS.format(label=short),
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


def counting_choices() -> list[tuple[str, str]]:
    """« Compte »'s three choices, each with its sentence (COUNTING_HELP)
    under its words - what the radio buttons draw."""
    return [
        (value, format_html('{}<span class="muted small counting-help">{}</span>', label, COUNTING_HELP[value]))
        for value, label in SaleDocument.Counting.choices
    ]


def source_of(line: SaleDocumentLine) -> str:
    """A line's choice of what it sold: « recipe:<pk> », « stock:<pk> », or
    "" for a line tied to nothing."""
    if line.recipe_id:
        return f"recipe:{line.recipe_id}"
    if line.stock_type_id:
        return f"stock:{line.stock_type_id}"
    return ""


def source_ids(value: str) -> tuple[int | None, int | None]:
    """(recipe id, article id) of a choice of `sale_source_choices` - one
    the field has checked."""
    kind, _, pk = (value or "").partition(":")
    if kind == "recipe":
        return int(pk), None
    if kind == "stock":
        return None, int(pk)
    return None, None


class SaleHeaderForm(forms.ModelForm):
    """What both pages of a sale document type in its header: the date it
    counts on, how it counts, a note.

    **Missing is not blank** (CLAUDE.md): a field of KEPT_WHEN_ABSENT the
    POST does not carry keeps the document's value - a page left open from
    before the sales invoices, posted after them, blanks nothing; a new
    document is « Compte dans les marges et le stock »."""

    KEPT_WHEN_ABSENT: tuple[str, ...] = ("counting",)
    #: The fields the page draws itself, outside _form_fields.html's grid:
    #: « Compte » is a group of choices, a fieldset and its legend.
    drawn_apart: tuple[str, ...] = ("counting",)

    sold_on = forms.DateField(
        label="Date de vente",
        help_text="Le jour où la vente compte, dans les marges et dans le stock.",
        widget=forms.DateInput(attrs={"type": "date", "min": f"{EARLIEST_DOCUMENT_DATE:%Y-%m-%d}"}),
        error_messages={"required": DATE_REQUIRED, "invalid": DATE_UNREADABLE},
    )
    counting = forms.ChoiceField(
        label="Compte",
        required=False,
        choices=counting_choices,
        widget=forms.RadioSelect,
        error_messages={"invalid_choice": UNKNOWN_CHOICE},
    )
    note = forms.CharField(label="Note", max_length=255, required=False, error_messages=_text_errors(255))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sold_on"].widget.attrs["max"] = timezone.localdate().isoformat()
        if not self.instance.pk:
            # self.initial, NOT fields["sold_on"].initial: a ModelForm seeds
            # self.initial from the instance, so the key is already there
            # holding None and the field's own initial is never consulted -
            # the date box just renders empty.
            self.initial["sold_on"] = timezone.localdate()

    def clean_sold_on(self):
        return check_document_date(self.cleaned_data.get("sold_on"))

    def clean_counting(self):
        """Posted blank - by hand: no radio sends it - is not a value."""
        return self.cleaned_data.get("counting") or self.instance.counting

    def clean(self):
        cleaned = super().clean()
        for name in self.KEPT_WHEN_ABSENT:
            if name in self.errors or name not in self.fields:
                continue
            if self.fields[name].widget.value_omitted_from_data(self.data, self.files, self.add_prefix(name)):
                cleaned[name] = getattr(self.instance, name)
        return cleaned


class SaleDocumentForm(SaleHeaderForm):
    """A typed sale document's header: every figure typed - its date, its
    number, its customer and the totals it prints, how it counts, a note -
    and a plain file kept with it.

    The file is a plain field OUTSIDE Meta.fields (`ManualInvoiceForm`'s
    way): in it, `construct_instance` would assign the upload and the field
    would write a second copy on `document.save()`. recipes/sale_files.py
    stores it."""

    KEPT_WHEN_ABSENT = ("customer", "counting", "stated_total_ttc", "stated_total_ht", "prepaid_ttc")

    reference = forms.CharField(label="Numéro", max_length=100, required=False, error_messages=_text_errors(100))
    customer = forms.CharField(
        label="Client",
        max_length=255,
        required=False,
        error_messages=_text_errors(255),
        widget=forms.TextInput(attrs={"list": KNOWN_CUSTOMERS_LIST, "autocomplete": "off"}),
    )
    stated_total_ttc = _total_field(
        "Total TTC de la facture", "Total TTC", "Facultatif : le total imprimé. Vide, c'est la somme des lignes."
    )
    stated_total_ht = _total_field(
        "Total HT de la facture",
        "Total HT",
        "Facultatif : imprimé sur la facture, il fait compter son montant en HT dans les marges.",
    )
    prepaid_ttc = _total_field("Déjà réglé (acompte)", "Déjà réglé", "Un acompte que la facture déduit de son total.")
    source_file = forms.FileField(
        label="Fichier de la facture",
        required=False,
        help_text="PDF, photo, Word, Excel, texte : 25 Mo au plus. Une facture électronique s'ajoute avec « Lire la "
        "facture ».",
        widget=forms.FileInput(attrs={"accept": SALE_FILE_ACCEPT}),
        error_messages={"invalid": FILE_MISSING, "missing": FILE_MISSING, "empty": FILE_EMPTY},
    )
    retirer_fichier = forms.BooleanField(label="Retirer le fichier", required=False)

    class Meta:
        model = SaleDocument
        fields = [
            "sold_on",
            "reference",
            "customer",
            "stated_total_ttc",
            "stated_total_ht",
            "prepaid_ttc",
            "counting",
            "note",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # « Retirer » only for a file there is.
        if not self.instance.source_file:
            del self.fields["retirer_fichier"]

    def clean_reference(self):
        """A number is its seller's, and the bar is the seller of every one:
        a number another document holds is refused - only when it CHANGED,
        so two documents saved with one number before still save."""
        reference = self.cleaned_data.get("reference", "")
        if not reference or "reference" not in self.changed_data:
            return reference
        holder = (
            SaleDocument.objects.filter(reference__iexact=reference)
            .exclude(pk=self.instance.pk)
            .order_by("-sold_on", "-pk")
            .first()
        )
        if holder is not None:
            raise ValidationError(NUMBER_TAKEN.format(number=holder.reference, day=f"{holder.sold_on:%d/%m/%Y}"))
        return reference

    def clean_source_file(self):
        upload = self.cleaned_data.get("source_file")
        if not upload:
            return upload
        if file_extension(upload.name) not in SALE_FILE_EXTENSIONS:
            raise ValidationError(EXTENSION_REFUSED)
        too_big = file_too_big(upload)
        if too_big:
            raise ValidationError(too_big)
        return upload

    def clean(self):
        cleaned = super().clean()
        ttc, ht = cleaned.get("stated_total_ttc"), cleaned.get("stated_total_ht")
        if ht is not None and "stated_total_ttc" not in self.errors:
            if ttc is None:
                self.add_error("stated_total_ht", HT_WITHOUT_TTC)
            elif ht * ttc < 0 or abs(ht) > abs(ttc):
                self.add_error("stated_total_ht", HT_ABOVE_TTC)
        return cleaned


class SaleEInvoiceHeaderForm(SaleHeaderForm):
    """An electronic invoice's header: only its date of sale, how it counts
    and a note change - its number, customer and totals are its data,
    printed, never typed (spec §5.3). Under the date, the dates the invoice
    states."""

    class Meta:
        model = SaleDocument
        fields = ["sold_on", "counting", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stated = []
        if self.instance.einvoice_issued_on:
            stated.append(f"Date de la facture électronique : {self.instance.einvoice_issued_on:%d/%m/%Y}")
        if self.instance.einvoice_delivered_on:
            stated.append(f"livraison : {self.instance.einvoice_delivered_on:%d/%m/%Y}")
        if stated:
            self.fields["sold_on"].help_text = " · ".join(stated)


def sale_source_choices(keep=None) -> list:
    """Everything sellable: a recipe, or a stock item sold as itself - and
    « rien », a line tied to nothing (« Location de salle »).

    A recipe with no price is a preparation and is NOT sellable - offered, a
    line naming it books its full cost against 0,00 € of revenue
    (`SaleDocumentLine.total_ttc` has nothing to fall back on) while
    `margins.computation` counts the cost, which is the asymmetry CLAUDE.md
    forbids for an article sold as itself: both sides out, or neither. A 0 is
    a price somebody typed, so a comped drink stays offered.

    `keep` - a recipe id, or several - puts them back: a line written before
    its recipe's price was cleared must still open, and a choice missing
    from the list is a form that refuses the document rather than a
    document that can be corrected. The formset and the tie grid keep every
    recipe the document's lines hold.
    """
    if keep is None:
        kept = []
    elif isinstance(keep, int):
        kept = [keep]
    else:
        kept = [pk for pk in keep if pk is not None]
    sellable = Q(selling_price_ttc__isnull=False)
    if kept:
        sellable |= Q(pk__in=kept)
    recipes = Recipe.objects.filter(sellable).order_by("name").values_list("pk", "name")
    return [
        ("", NO_SOURCE),
        ("Recettes", [(f"recipe:{pk}", name) for pk, name in recipes]),
        (
            "Articles",
            [(f"stock:{st.pk}", f"{st.name} ({st.get_unit_display()})") for st in StockType.objects.order_by("name")],
        ),
    ]


class SaleDocumentLineForm(BlankRowTolerantModelForm):
    """One typed line: what it says (« Libellé »), what it sold - a recipe,
    an article, or nothing (`source`, one field for « what did you sell? »,
    the RecipeIngredientForm shape) -, its quantity and price, its rate
    (« TVA % »), and what it consumed when that is not what was invoiced.

    Every figure read as a person types it (« 12,50 »), every refusal in
    French. The line's own rules (a label when tied to nothing, a consumed
    quantity only when tied) are SaleDocumentLine.clean's, said for the row.
    A tie changed with the consumed quantity left as drawn clears it: 30
    typed in litres of a keg is not 30 cocktails (`cleared`, said by the
    view)."""

    label = forms.CharField(
        label="Libellé",
        max_length=255,
        required=False,
        error_messages={"max_length": LABEL_TOO_LONG, "null_characters_not_allowed": NUL_REFUSED},
    )
    source = forms.ChoiceField(
        label="Correspond à", required=False, widget=SelectWidget, error_messages={"invalid_choice": UNKNOWN_CHOICE}
    )
    quantity = TypedAmountField(
        label="Quantité",
        required=True,
        places=4,
        digits=10,
        unreadable=QUANTITY_UNREADABLE,
        error_messages={"required": QUANTITY_REQUIRED},
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    unit_price_ttc = TypedAmountField(
        label="Prix unitaire TTC",
        places=2,
        digits=8,
        unreadable=PRICE_UNREADABLE,
        ambiguous=PRICE_AMBIGUOUS,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    vat_percent = TypedAmountField(
        label="TVA %",
        places=2,
        digits=5,
        unreadable=RATE_REFUSED,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )
    consumed_quantity = TypedAmountField(
        label="Quantité consommée",
        places=4,
        digits=10,
        unreadable=CONSUMED_UNREADABLE,
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )

    bookkeeping_fields = ()

    class Meta:
        model = SaleDocumentLine
        fields = ["label", "quantity", "unit_price_ttc", "consumed_quantity"]

    def __init__(self, *args, source_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The formset builds these once, with every recipe its document's
        # lines hold kept (see BaseSaleDocumentLineFormSet); the fallback is
        # for a form used on its own, e.g. in a test.
        line = self.instance
        self.fields["source"].choices = (
            source_choices
            if source_choices is not None
            else sale_source_choices(keep=line.recipe_id if line.pk else None)
        )
        if line.pk:
            self.initial["source"] = source_of(line)
            if line.vat_rate is not None:
                # Drawn at two decimals, as the invoices' rates are (CLAUDE.md
                # « The rate is drawn at two decimals »): « 20.00 », not the
                # stored fraction's « 20.0000 ».
                self.initial["vat_percent"] = (line.vat_rate * HUNDRED).quantize(CENTS_PLACES)
        if line.recipe_id and line.recipe.selling_price_ttc is not None:
            menu = format(line.recipe.selling_price_ttc, "f").replace(".", ",")
            self.fields["unit_price_ttc"].widget.attrs["placeholder"] = f"prix de la carte : {menu} €"
        #: Whether the save gives the consumed quantity back to the invoiced one.
        self.cleared = False

    def clean_vat_percent(self):
        rate = self.cleaned_data.get("vat_percent")
        if rate is not None and not 0 <= rate <= HUNDRED:
            raise ValidationError(RATE_REFUSED)
        return rate

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE") or "source" in self.errors:
            return cleaned
        line = self.instance
        stored_consumed = line.consumed_quantity
        recipe_id, stock_type_id = source_ids(cleaned.get("source") or "")
        line.recipe_id, line.stock_type_id = recipe_id, stock_type_id
        rate = cleaned.get("vat_percent")
        line.vat_rate = (rate / HUNDRED).quantize(RATE_PLACES) if rate is not None else None
        tied = bool(recipe_id or stock_type_id)
        price = cleaned.get("unit_price_ttc")
        if "unit_price_ttc" not in self.errors:
            if not tied and price is None:
                self.add_error("unit_price_ttc", FREE_LINE_PRICE)
            elif stock_type_id and rate is not None and price is None:
                self.add_error("unit_price_ttc", ARTICLE_RATE_PRICE)
            elif tied and price is not None and price < 0:
                self.add_error("unit_price_ttc", NEGATIVE_PRICE)
        if "consumed_quantity" in self.errors:
            return cleaned
        consumed, quantity = cleaned.get("consumed_quantity"), cleaned.get("quantity")
        if (
            line.pk
            and stored_consumed is not None
            and "source" in self.changed_data
            and "consumed_quantity" not in self.changed_data
        ):
            cleaned["consumed_quantity"] = None
            self.cleared = True
        elif tied and consumed and quantity and (consumed > 0) != (quantity > 0):
            self.add_error("consumed_quantity", CONSUMED_SIGN_TYPED)
        return cleaned


class BaseSaleDocumentLineFormSet(BaseInlineFormSet):
    """A typed document's lines: no « Ajoutez au moins une ligne » here - a
    total typed alone is a document (the view refuses neither a line nor a
    total, NOTHING_SOLD) -, MAX_SALE_LINES rows at most, every refusal in
    French, a line deleted in another tab said on its row."""

    default_error_messages = {
        "too_many_forms": TOO_MANY_LINES_TYPED,
        "missing_management_form": LINES_FORM_BROKEN,
    }

    def __init__(self, *args, **kwargs):
        # A recipe line's placeholder names its menu price: the recipes and
        # articles come with the lines, not one query a row.
        kwargs.setdefault("queryset", SaleDocumentLine.objects.select_related("recipe", "stock_type"))
        super().__init__(*args, **kwargs)

    @cached_property
    def source_choices(self):
        """Built once for every row - two full scans - with every recipe the
        saved lines hold kept: a line on a recipe since turned preparation
        opens and saves untouched (it never reached the formset before
        05/10/2026)."""
        keep = {line.recipe_id for line in self.get_queryset() if line.recipe_id}
        return SharedChoices(sale_source_choices(keep))

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs.setdefault("source_choices", self.source_choices)
        return kwargs

    def add_fields(self, form, index):
        """The row's own hidden fields - its id, its document's - refused in
        French, and printed with the row: a hidden field's error shows
        nowhere by default (CLAUDE.md « An id read from request.POST »)."""
        super().add_fields(form, index)
        for name in (self.model._meta.pk.name, self.fk.name):
            if name in form.fields:
                form.fields[name].error_messages.update(required=LINE_GONE, invalid_choice=LINE_GONE)

    @cached_property
    def empty_form(self):
        """The spare row the page's <template> holds, built once."""
        return super().empty_form


#: A NEW document opens with one row to type; a saved one with its lines
#: only (CLAUDE.md « Formsets: no spare row on a saved record »).
SaleDocumentLineFormSetNew = inlineformset_factory(
    SaleDocument,
    SaleDocumentLine,
    form=SaleDocumentLineForm,
    formset=BaseSaleDocumentLineFormSet,
    fields=["label", "quantity", "unit_price_ttc", "consumed_quantity"],
    extra=1,
    max_num=MAX_SALE_LINES,
    validate_max=True,
    can_delete=True,
)
SaleDocumentLineFormSet = inlineformset_factory(
    SaleDocument,
    SaleDocumentLine,
    form=SaleDocumentLineForm,
    formset=BaseSaleDocumentLineFormSet,
    fields=["label", "quantity", "unit_price_ttc", "consumed_quantity"],
    extra=0,
    max_num=MAX_SALE_LINES,
    validate_max=True,
    can_delete=True,
)


@dataclass
class TieRow:
    """One line of an electronic invoice as its grid draws it."""

    line: SaleDocumentLine
    #: Its « Correspond à » and « Quantité consommée » - None for a line that
    #: is never tied.
    tie: BoundField | None
    consumed: BoundField | None
    #: Why it has no select (rebuilt, the mirror shape), "" otherwise.
    blocked: str
    #: Why a proposal is drawn, "" without one.
    why: str
    #: « Quantité consommée ? … », said under its cell.
    doubt: str
    #: Its consumed quantity stored as 0: nothing left the stock.
    zero: bool

    @property
    def rate(self) -> str:
        """« 20 % » - a rate as a person reads it."""
        return "" if self.line.vat_rate is None else f"{plain_number(self.line.vat_rate * HUNDRED)} %"


@dataclass
class TiesSaved:
    """What a save of the grid did: how many lines it tied, and the lines
    whose consumed quantity a new tie gave back to the invoiced one."""

    tied: int = 0
    cleared: list[str] = field(default_factory=list)


class SaleTiesForm(forms.Form):
    """An electronic invoice's lines tied to what they sold: `lien-<pk>` (a
    recipe, an article, or « rien ») and `consomme-<pk>` (what it consumed,
    blank: the quantity invoiced) for each line that may be tied. A grid
    named by line pk, never a formset - no index can move a value onto
    another line (the timesheet grid's rule). A line rebuilt from a VAT table
    and one of the mirror shape have neither: a tie posted for one is
    refused.

    `save()` writes, in one update, only what the POST says differently from
    what is stored; a field the POST leaves out is « not said » - the stored
    tie stays. A tie changed with the consumed quantity left as drawn clears
    it. `proposals` (recipes/sale_lines.py) are drawn as the initial choice,
    on a GET only; `unit_costs` ({article id: unit cost}) are what the
    consumption doubt compares an article's line with."""

    def __init__(self, document, lines, data=None, proposals=None, unit_costs=None):
        super().__init__(data)
        self.document = document
        self.lines = list(lines)
        self.proposals = proposals or {}
        self.unit_costs = unit_costs or {}
        self.decided: dict[int, tuple] = {}
        choices = SharedChoices(sale_source_choices({line.recipe_id for line in self.lines if line.recipe_id}))
        for line in self.lines:
            if not tieable(line):
                continue
            tie, consumed = self.names(line)
            self.fields[tie] = forms.ChoiceField(
                label="Correspond à",
                choices=choices,
                required=False,
                widget=SelectWidget(attrs={"aria-label": f"Ce que vend la ligne « {line.label} »"}),
                error_messages={"invalid_choice": TIE_UNKNOWN},
            )
            self.fields[consumed] = TypedAmountField(
                label="Quantité consommée",
                places=4,
                digits=10,
                unreadable=CONSUMED_UNREADABLE,
                widget=forms.TextInput(
                    attrs={
                        "inputmode": "decimal",
                        "aria-label": f"Quantité consommée pour « {line.label} »",
                        "placeholder": plain_number(line.quantity),
                    }
                ),
            )
            proposal = self.proposals.get(line.pk)
            if proposal is not None and not line.is_tied:
                self.initial[tie] = proposal.value
                self.initial[consumed] = proposal.consumed
            else:
                self.initial[tie] = source_of(line)
                self.initial[consumed] = line.consumed_quantity

    @staticmethod
    def names(line) -> tuple[str, str]:
        return f"lien-{line.pk}", f"consomme-{line.pk}"

    def clean(self):
        cleaned = super().clean()
        self.decided = {}
        for line in self.lines:
            tie, consumed_name = self.names(line)
            if not tieable(line):
                if self.data.get(tie):
                    self.add_error(None, REBUILT_NOT_TIED if line.rebuilt else MIRROR_NOT_TIED)
                continue
            if tie in self.errors or consumed_name in self.errors:
                continue
            said_tie, said_consumed = tie in self.data, consumed_name in self.data
            recipe_id, stock_type_id = (
                source_ids(cleaned.get(tie, "")) if said_tie else (line.recipe_id, line.stock_type_id)
            )
            consumed = cleaned.get(consumed_name) if said_consumed else line.consumed_quantity
            cleared = False
            if (
                (recipe_id, stock_type_id) != (line.recipe_id, line.stock_type_id)
                and line.consumed_quantity is not None
                and consumed == line.consumed_quantity
            ):
                consumed, cleared = None, True
            if consumed is not None:
                if not (recipe_id or stock_type_id):
                    self.add_error(consumed_name, LINE_CONSUMED_UNTIED)
                    continue
                money = line.total_ht if line.total_ht is not None else line.quantity
                if consumed and money and (consumed > 0) != (money > 0):
                    self.add_error(consumed_name, CONSUMED_SIGN_TIE)
                    continue
            self.decided[line.pk] = (recipe_id, stock_type_id, consumed, cleared)
        return cleaned

    def save(self) -> TiesSaved:
        saved = TiesSaved()
        changed = []
        for line in self.lines:
            if line.pk not in self.decided:
                continue
            recipe_id, stock_type_id, consumed, cleared = self.decided[line.pk]
            if (recipe_id, stock_type_id, consumed) == (line.recipe_id, line.stock_type_id, line.consumed_quantity):
                continue
            if (recipe_id or stock_type_id) and (recipe_id, stock_type_id) != (line.recipe_id, line.stock_type_id):
                saved.tied += 1
            line.recipe_id, line.stock_type_id, line.consumed_quantity = recipe_id, stock_type_id, consumed
            changed.append(line)
            if cleared:
                saved.cleared.append(line.shown_name)
        SaleDocumentLine.objects.bulk_update(changed, ["recipe", "stock_type", "consumed_quantity"])
        return saved

    @property
    def rows(self) -> list[TieRow]:
        rows = []
        for line in self.lines:
            if not tieable(line):
                rows.append(TieRow(line, None, None, REBUILT_LINE if line.rebuilt else MIRROR_LINE, "", "", False))
                continue
            tie, consumed = self.names(line)
            proposal = None if self.is_bound or line.is_tied else self.proposals.get(line.pk)
            rows.append(
                TieRow(
                    line=line,
                    tie=self[tie],
                    consumed=self[consumed],
                    blocked="",
                    why=proposal.why if proposal is not None else "",
                    doubt=consumption_doubt(line, self.unit_costs.get(line.stock_type_id)) if line.is_tied else "",
                    zero=line.is_tied and line.consumed_quantity == 0,
                )
            )
        return rows


# -- « Import automatique des ventes » (recipes/auto_sales.py) -------------------------------------------------------

AUTO_SALES_NUL_REFUSED = "Caractère interdit (NUL) : retapez ce champ."
AUTO_SALES_NAME_REQUIRED = "Donnez un nom à cet import."
AUTO_SALES_NO_DAY = "Cochez au moins un jour."
AUTO_SALES_UNKNOWN_CHOICE = "Choix inconnu : rechargez la page."
AUTO_SALES_TOO_MANY_TIMES = "6 heures au plus."


class AutoSalesImportForm(forms.Form):
    """An automatic sales import's settings. Its days are drawn by hand
    (auto_sales.html: a fieldset of seven boxes, calendar days) from
    `day_rows`; its source is a select of recipes/sales_sources.py. Saved
    through `values()`, the form's fields only: the scheduler's columns
    (`last_slot_at`, `last_result`, `last_failed`) are never written here."""

    name = forms.CharField(
        label="Nom",
        max_length=80,
        error_messages={
            "required": AUTO_SALES_NAME_REQUIRED,
            "max_length": "80 caractères au plus.",
            "null_characters_not_allowed": AUTO_SALES_NUL_REFUSED,
        },
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    source = forms.ChoiceField(
        label="Source",
        error_messages={"required": AUTO_SALES_UNKNOWN_CHOICE, "invalid_choice": AUTO_SALES_UNKNOWN_CHOICE},
    )
    weekdays = forms.MultipleChoiceField(
        label="Jours",
        required=False,
        choices=[(str(day), str(day)) for day in range(7)],
        error_messages={"invalid_choice": AUTO_SALES_UNKNOWN_CHOICE, "invalid_list": AUTO_SALES_UNKNOWN_CHOICE},
    )
    times = forms.CharField(
        label="Heures",
        max_length=120,
        help_text="ex. 07:00, ou 07:00 12:00",
        error_messages={
            "required": "Indiquez au moins une heure.",
            "max_length": "120 caractères au plus.",
            "null_characters_not_allowed": AUTO_SALES_NUL_REFUSED,
        },
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    is_active = forms.BooleanField(label="Actif", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .sales_sources import choices

        self.fields["source"].choices = choices()

    @staticmethod
    def initial_for(rule) -> dict:
        try:
            days = [str(day) for day in rule.weekday_list()]
        except ValueError:
            days = []
        return {
            "name": rule.name,
            "source": rule.source,
            "weekdays": days,
            "times": rule.times,
            "is_active": rule.is_active,
        }

    def day_rows(self) -> list[dict]:
        from staff.timesheet import DAY_NAMES

        value = self["weekdays"].value() or []
        ticked = {str(item) for item in (value if isinstance(value, (list, tuple)) else [value])}
        return [
            {
                "value": str(day),
                "label": DAY_NAMES[day].lower(),
                "checked": str(day) in ticked,
                "id": f"{self['weekdays'].auto_id}_{day}",
            }
            for day in range(7)
        ]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError(AUTO_SALES_NAME_REQUIRED)
        return name

    def clean_weekdays(self):
        days = sorted({int(day) for day in self.cleaned_data["weekdays"]})
        if not days:
            raise forms.ValidationError(AUTO_SALES_NO_DAY)
        return days

    def clean_times(self):
        from notifications import schedule

        from .models import AutoSalesImport

        try:
            times = schedule.parse_times(self.cleaned_data["times"])
        except ValueError as exc:
            message = str(exc)
            raise forms.ValidationError(
                AUTO_SALES_TOO_MANY_TIMES if message == schedule.TOO_MANY_TIMES else message
            ) from None
        if len(times) > AutoSalesImport.MAX_TIMES:
            raise forms.ValidationError(AUTO_SALES_TOO_MANY_TIMES)
        return times

    def values(self) -> dict:
        """The model's fields this form owns, from a valid form."""
        from notifications import schedule

        data = self.cleaned_data
        return {
            "name": data["name"],
            "source": data["source"],
            "weekdays": schedule.weekdays_value(data["weekdays"]),
            "times": schedule.times_value(data["times"]),
            "is_active": data["is_active"],
        }
