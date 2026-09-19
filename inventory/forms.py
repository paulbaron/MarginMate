from django import forms
from django.forms import inlineformset_factory

from .models import Product, StockTake, StockTakeLine, StockType, UnitChoices
from .services import first_purchase_dates, product_counting_ratios


class StockTypeForm(forms.ModelForm):
    class Meta:
        model = StockType
        fields = ["name", "unit", "category", "loss_percent"]
        # LANGUAGE_CODE is en-us, so an unlabelled field renders its English
        # attribute name ("Loss percent") in an otherwise French interface -
        # same reason StockTakeForm spells its labels out.
        labels = {
            "name": "Nom",
            "unit": "Unité",
            "category": "Catégorie",
            "loss_percent": "Perte estimée (%)",
        }
        widgets = {
            "category": forms.TextInput(attrs={"list": "category-datalist", "autocomplete": "off"}),
        }
        # Django's own is "Stock type with this Name already exists.", in
        # English, and it is what a rename collision printed on the page.
        error_messages = {"name": {"unique": "Un article porte déjà ce nom."}}


def product_display_name(product: Product) -> str:
    return f"{product.raw_name} — {product.supplier.name}"


STOCK_TYPE_ENTRY_SUFFIX = " (article)"
# What the suffix read before a StockType became an « article » (19/09). A
# count in progress is kept in the browser as it was typed (the draft net of
# stock_take_form.html), so an entry carrying it has to keep resolving.
OLD_STOCK_TYPE_ENTRY_SUFFIXES = (" (type de stock)",)


def stock_type_entry_name(stock_type: StockType) -> str:
    return f"{stock_type.name}{STOCK_TYPE_ENTRY_SUFFIX}"


def current_entry_name(name: str) -> str:
    """`name` with an old stock-item suffix put back to today's one."""
    for old in OLD_STOCK_TYPE_ENTRY_SUFFIXES:
        if name.endswith(old):
            return name[: -len(old)] + STOCK_TYPE_ENTRY_SUFFIX
    return name


def is_stock_type_entry(name: str) -> bool:
    return current_entry_name(name).endswith(STOCK_TYPE_ENTRY_SUFFIX)


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


class EntryResolver:
    """Turns the text typed into a stock-take row back into the Product or
    StockType it names.

    Loaded once and shared by every row of a formset. Resolving one row at a
    time meant a query per row, so a 400-line inventory spent 400 queries
    just deciding what the user had typed, before a single line was valued.

    It also answers "did this exist yet", which is the same question asked of
    every row against the same date - see first_purchase_dates.
    """

    def __init__(self):
        self._products = None
        self._stock_types = None
        self._first_purchases = None

    def _load(self):
        if self._products is not None:
            return
        products = list(
            Product.objects.select_related("supplier", "stock_type").filter(stock_type__isnull=False)
        )
        self._products = {product_display_name(product): product for product in products}
        self._stock_types = {stock_type_entry_name(st): st for st in StockType.objects.all()}
        self._first_purchases = first_purchase_dates([product.id for product in products])

    def product(self, name: str) -> Product | None:
        self._load()
        return self._products.get(name)

    def stock_type(self, name: str) -> StockType | None:
        self._load()
        return self._stock_types.get(current_entry_name(name))

    def first_purchase(self, product: Product):
        """When this product was first delivered, or None if nothing dated
        says - in which case there is no ground to call it too new."""
        self._load()
        return self._first_purchases.get(product.id)

    def stock_type_first_purchase(self, stock_type: StockType):
        """The earliest delivery of ANY product under this stock item: the
        stock item existed from the moment its first bottle arrived,
        whichever brand that was."""
        self._load()
        dates = [
            self._first_purchases[product.id]
            for product in self._products.values()
            if product.stock_type_id == stock_type.id and product.id in self._first_purchases
        ]
        return min(dates) if dates else None


def stock_take_entry_lookup() -> dict[str, dict]:
    """{"display text": {"kind": "product"|"stock_type", "unit_choices":
    [[value, label], ...], "default_unit": "UNIT"}, ...} - every product or
    stock type a stock-take line can be counted against, keyed by the text
    shown in the shared datalist (the same "free-typed name matched
    against a datalist" pattern the review queue's stock-item field uses),
    so the template/JS can offer the right unit choices once one gets
    typed in. Resolving the typed text back to an actual Product/StockType
    - and validating the submitted unit is actually one of its allowed
    choices - happens separately in StockTakeLineForm.clean(), through the
    shared EntryResolver rather than through this (comparatively expensive -
    it walks every product's invoice history) map.

    `available_from` is the ISO date the thing was first delivered, or None
    when nothing dated says. The page uses it to keep entries that didn't
    exist yet out of the datalist for the date being counted, so the shape of
    the list follows the date field as the user changes it - which is why the
    dates are shipped to the browser rather than filtered here."""
    products = list(Product.objects.select_related("supplier", "stock_type").filter(stock_type__isnull=False))
    ratios = product_counting_ratios([p.id for p in products])
    first_purchases = first_purchase_dates([product.id for product in products])
    entries = {}
    for product in products:
        is_discrete = len(ratios.get(product.id, set())) <= 1
        choices, default = _unit_choices_for_product(product, is_discrete)
        first = first_purchases.get(product.id)
        entries[product_display_name(product)] = {
            "kind": "product",
            "unit_choices": choices,
            "default_unit": default,
            "available_from": first.isoformat() if first else None,
        }
    # A stock item exists from the moment its first bottle arrived, whichever
    # brand that was - so the earliest purchase across every product under it.
    earliest_by_type: dict[int, object] = {}
    for product in products:
        first = first_purchases.get(product.id)
        if first is None:
            continue
        current = earliest_by_type.get(product.stock_type_id)
        if current is None or first < current:
            earliest_by_type[product.stock_type_id] = first
    for stock_type in StockType.objects.all():
        first = earliest_by_type.get(stock_type.id)
        entries[stock_type_entry_name(stock_type)] = {
            "kind": "stock_type",
            "unit_choices": [[stock_type.unit, stock_type.get_unit_display()]],
            "default_unit": stock_type.unit,
            "available_from": first.isoformat() if first else None,
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
        label="Produit ou article",
        required=True,
        widget=forms.TextInput(attrs={"list": "stock-take-entry-datalist", "autocomplete": "off"}),
    )

    class Meta:
        model = StockTakeLine
        fields = ["counted_quantity", "unit"]
        labels = {"counted_quantity": "Quantité comptée", "unit": "Unité"}

    def __init__(self, *args, as_of=None, resolver=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The date being counted, and a lookup shared by every row of the
        # formset - both handed down by _stock_take_form_view. `as_of` is the
        # date SUBMITTED with this save, not the one on the saved instance:
        # changing the date and adding a row happen in the same POST, and the
        # new row has to be judged against the new date.
        self.as_of = as_of
        self.resolver = resolver if resolver is not None else EntryResolver()
        self.fields["unit"].required = False  # resolved/validated in clean() against the chosen entry instead
        if self.instance.pk and self.instance.product_id:
            self.initial["entry_search"] = product_display_name(self.instance.product)
        elif self.instance.pk and self.instance.stock_type_id:
            self.initial["entry_search"] = stock_type_entry_name(self.instance.stock_type)

    def _too_new(self, first_purchase) -> bool:
        """Whether this was first delivered after the date being counted.

        Nothing dated on record means no grounds to refuse it - see
        services.first_purchase_dates.
        """
        return self.as_of is not None and first_purchase is not None and first_purchase > self.as_of

    def clean(self):
        cleaned = super().clean()
        if self.cleaned_data.get("DELETE"):
            return cleaned
        name = (cleaned.get("entry_search") or "").strip()
        if not name:
            self.add_error("entry_search", "Choisissez un produit ou un article.")
            return cleaned
        unit = cleaned.get("unit")
        if is_stock_type_entry(name):
            stock_type = self.resolver.stock_type(name)
            if stock_type is None:
                self.add_error("entry_search", "Article introuvable - choisissez-en un dans la liste proposée.")
                return cleaned
            first = self.resolver.stock_type_first_purchase(stock_type)
            if self._too_new(first):
                self.add_error("entry_search", self._too_new_message(first))
                return cleaned
            self.instance.stock_type = stock_type
            self.instance.product = None
            self.instance.unit = stock_type.unit  # no real choice for a stock-type line
            return cleaned
        product = self.resolver.product(name)
        if product is None:
            self.add_error("entry_search", "Introuvable - choisissez un élément dans la liste proposée.")
            return cleaned
        if self._too_new(self.resolver.first_purchase(product)):
            self.add_error("entry_search", self._too_new_message(self.resolver.first_purchase(product)))
            return cleaned
        allowed_units = {UnitChoices.UNIT, product.stock_type.unit}
        if unit not in allowed_units:
            self.add_error("unit", "Choisissez l'unité dans laquelle vous avez compté ce produit.")
            return cleaned
        self.instance.product = product
        self.instance.stock_type = None
        self.instance.unit = unit
        return cleaned

    def _too_new_message(self, first_purchase) -> str:
        return (
            f"Première livraison le {first_purchase:%d/%m/%Y}, après la date de cet inventaire "
            f"({self.as_of:%d/%m/%Y}) - il ne pouvait pas être en stock ce jour-là."
        )


StockTakeLineFormSet = inlineformset_factory(
    StockTake,
    StockTakeLine,
    form=StockTakeLineForm,
    fields=["counted_quantity", "unit"],
    # No spare row. With extra=1 the form always rendered one blank line, so
    # taking an item out of a saved inventory and reopening it showed the
    # remaining items PLUS an empty slot - which reads exactly like the
    # removal half-failed, and was reported as such. Rows are added by the
    # "+ Ajouter une ligne" button instead, and the page starts a brand-new
    # inventory off with one (see stock_take_form.html), so what is on screen
    # is only ever what is really in the count.
    extra=0,
    can_delete=True,
)
