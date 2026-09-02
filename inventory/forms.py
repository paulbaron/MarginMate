from django import forms
from django.forms import inlineformset_factory

from .models import Product, StockTake, StockTakeLine, StockType, UnitChoices
from .services import product_counting_ratios


class StockTypeForm(forms.ModelForm):
    class Meta:
        model = StockType
        fields = ["name", "unit", "category"]
        widgets = {
            "category": forms.TextInput(attrs={"list": "category-datalist", "autocomplete": "off"}),
        }


def product_display_name(product: Product) -> str:
    return f"{product.raw_name} — {product.supplier.name}"


STOCK_TYPE_ENTRY_SUFFIX = " (type de stock)"


def stock_type_entry_name(stock_type: StockType) -> str:
    return f"{stock_type.name}{STOCK_TYPE_ENTRY_SUFFIX}"


def _unit_choices_for_product(product: Product, is_discrete: bool) -> tuple[list[list[str]], str]:
    """(unit_choices, default_unit) for one product - UNIT is always
    offered (any product can be counted as discrete items), plus the
    product's stock type's own unit when that's not already UNIT (so a
    count can instead be entered as a direct measurement, e.g. "roughly
    0.3L left in an open bottle" - see StockTakeLine.unit). Defaults to
    whichever product_is_discrete_count/bulk_product_counting_units would
    have auto-picked, but the user can still choose the other option."""
    stock_unit = product.stock_type.unit
    if stock_unit == UnitChoices.UNIT:
        return [[UnitChoices.UNIT, "Unité"]], UnitChoices.UNIT
    choices = [
        [UnitChoices.UNIT, "Unité (bouteilles/packs)"],
        [stock_unit, f"{product.stock_type.get_unit_display()} (mesuré directement)"],
    ]
    default = UnitChoices.UNIT if is_discrete else stock_unit
    return choices, default


def stock_take_entry_lookup() -> dict[str, dict]:
    """{"display text": {"kind": "product"|"stock_type", "unit_choices":
    [[value, label], ...], "default_unit": "UNIT"}, ...} - every product or
    stock type a stock-take line can be counted against, keyed by the text
    shown in the shared datalist (the same "free-typed name matched
    against a datalist" pattern the review queue's stock-item field uses),
    so the template/JS can offer the right unit choices once one gets
    typed in. Resolving the typed text back to an actual Product/StockType
    - and validating the submitted unit is actually one of its allowed
    choices - happens separately in StockTakeLineForm.clean(), with cheap
    per-row lookups rather than through this (comparatively expensive - it
    walks every product's invoice history) map."""
    products = list(Product.objects.select_related("supplier", "stock_type").filter(stock_type__isnull=False))
    ratios = product_counting_ratios([p.id for p in products])
    entries = {}
    for product in products:
        is_discrete = len(ratios.get(product.id, set())) <= 1
        choices, default = _unit_choices_for_product(product, is_discrete)
        entries[product_display_name(product)] = {"kind": "product", "unit_choices": choices, "default_unit": default}
    for stock_type in StockType.objects.all():
        entries[stock_type_entry_name(stock_type)] = {
            "kind": "stock_type",
            "unit_choices": [[stock_type.unit, stock_type.get_unit_display()]],
            "default_unit": stock_type.unit,
        }
    return entries


class StockTakeForm(forms.ModelForm):
    class Meta:
        model = StockTake
        fields = ["taken_at", "note"]
        labels = {"taken_at": "Date", "note": "Note"}
        widgets = {"taken_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}


class StockTakeLineForm(forms.ModelForm):
    entry_search = forms.CharField(
        label="Produit ou type de stock",
        required=True,
        widget=forms.TextInput(attrs={"list": "stock-take-entry-datalist", "autocomplete": "off"}),
    )

    class Meta:
        model = StockTakeLine
        fields = ["counted_quantity", "unit"]
        labels = {"counted_quantity": "Quantité comptée", "unit": "Unité"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].required = False  # resolved/validated in clean() against the chosen entry instead
        if self.instance.pk and self.instance.product_id:
            self.initial["entry_search"] = product_display_name(self.instance.product)
        elif self.instance.pk and self.instance.stock_type_id:
            self.initial["entry_search"] = stock_type_entry_name(self.instance.stock_type)

    def clean(self):
        cleaned = super().clean()
        if self.cleaned_data.get("DELETE"):
            return cleaned
        name = (cleaned.get("entry_search") or "").strip()
        if not name:
            self.add_error("entry_search", "Choisissez un produit ou un type de stock.")
            return cleaned
        unit = cleaned.get("unit")
        if name.endswith(STOCK_TYPE_ENTRY_SUFFIX):
            stock_type = StockType.objects.filter(name=name[: -len(STOCK_TYPE_ENTRY_SUFFIX)]).first()
            if stock_type is None:
                self.add_error("entry_search", "Type de stock introuvable - choisissez-en un dans la liste proposée.")
                return cleaned
            self.instance.stock_type = stock_type
            self.instance.product = None
            self.instance.unit = stock_type.unit  # no real choice for a stock-type line
            return cleaned
        product = Product.objects.select_related("stock_type").filter(
            supplier__name=name.rsplit(" — ", 1)[-1] if " — " in name else None,
            raw_name=name.rsplit(" — ", 1)[0] if " — " in name else name,
        ).first()
        if product is None:
            self.add_error("entry_search", "Introuvable - choisissez un élément dans la liste proposée.")
            return cleaned
        allowed_units = {UnitChoices.UNIT, product.stock_type.unit}
        if unit not in allowed_units:
            self.add_error("unit", "Choisissez l'unité dans laquelle vous avez compté ce produit.")
            return cleaned
        self.instance.product = product
        self.instance.stock_type = None
        self.instance.unit = unit
        return cleaned


StockTakeLineFormSet = inlineformset_factory(
    StockTake,
    StockTakeLine,
    form=StockTakeLineForm,
    fields=["counted_quantity", "unit"],
    extra=1,
    can_delete=True,
)
