from datetime import date

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

# The entry vocabulary and matcher live in entries.py, shared with the
# shopping lists; every name taken here is used here (the stock take's tests
# also import product_display_name, stock_type_entry_name and EntryResolver
# from this module).
from .entries import (
    ARTICLE_KIND,
    ITEMS,
    PRODUCT_KIND,
    EntryResolver,
    article_units,
    is_stock_type_entry,
    product_display_name,
    product_units,
    stock_type_entry_name,
)
from .models import Product, StockTake, StockTakeLine, StockType, UnitChoices
from .services import first_purchase_dates, is_discrete_count, product_counting_ratios


class StockTypeForm(forms.ModelForm):
    class Meta:
        model = StockType
        fields = ["name", "unit", "category", "loss_percent", "count_in_products_margin"]
        # LANGUAGE_CODE is en-us, so an unlabelled field renders its English
        # attribute name ("Loss percent") in an otherwise French interface -
        # same reason StockTakeForm spells its labels out.
        labels = {
            "name": "Nom",
            "unit": "Unité",
            "category": "Catégorie",
            "loss_percent": "Perte estimée (%)",
            "count_in_products_margin": "Compter dans la marge produits",
        }
        widgets = {
            "category": forms.TextInput(attrs={"list": "category-datalist", "autocomplete": "off"}),
        }
        # Django's own is "Stock type with this Name already exists.", in
        # English, and it is what a rename collision printed on the page.
        error_messages = {"name": {"unique": "Un article porte déjà ce nom."}}


def _unit_choices_for_product(product: Product, is_discrete: bool) -> tuple[list[list[str]], str]:
    """(unit_choices, default_unit) for one product - UNIT is always
    offered (any product can be counted as discrete items), plus the
    product's stock type's own unit when that's not already UNIT (so a
    count can instead be entered as a direct measurement, e.g. "roughly
    0.3L left in an open bottle" - see StockTakeLine.unit). Defaults to
    whichever product_is_discrete_count/bulk_product_counting_units would
    have auto-picked, but the user can still choose the other option.

    The rule is `entries.product_units`, shared with the shopping lists;
    the labels are the stock take's own."""
    units = product_units(product, is_discrete=is_discrete)
    if units.choices == (ITEMS,):
        return [[ITEMS, "Unité"]], units.default
    labels = {
        ITEMS: "Unité (bouteilles/packs)",
        product.stock_type.unit: f"{product.stock_type.get_unit_display()} (mesuré directement)",
    }
    return [[value, labels[value]] for value in units.choices], units.default


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
    # Only what an entry is made of: read whole, every product's suggestion
    # and its supplier's identifiers were decoded for nothing, a third of
    # what this took.
    products = list(
        Product.objects.select_related("supplier", "stock_type")
        .filter(stock_type__isnull=False)
        .only("raw_name", "supplier__name", "stock_type__unit")
    )
    ratios = product_counting_ratios([p.id for p in products])
    first_purchases = first_purchase_dates([product.id for product in products])
    entries = {}
    for product in products:
        choices, default = _unit_choices_for_product(product, is_discrete_count(ratios, product.id))
        first = first_purchases.get(product.id)
        entries[product_display_name(product)] = {
            "kind": PRODUCT_KIND,
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
        # Counted in its own unit only (no flag passed), so each choice is
        # labelled the way the article's unit reads.
        units = article_units(stock_type)
        entries[stock_type_entry_name(stock_type)] = {
            "kind": ARTICLE_KIND,
            "unit_choices": [[value, stock_type.get_unit_display()] for value in units.choices],
            "default_unit": units.default,
            "available_from": first.isoformat() if first else None,
        }
    return entries


#: The oldest date a count may carry: the invoices' own (2000).
EARLIEST_STOCK_TAKE_DATE = date(2000, 1, 1)


class StockTakeForm(forms.ModelForm):
    class Meta:
        model = StockTake
        fields = ["taken_at", "note"]
        labels = {"taken_at": "Date", "note": "Note"}
        widgets = {"taken_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean_taken_at(self):
        """Between 2000 and the end of next year. 9999-12-31 was saved, and
        « Combler les écarts » then read the day after it."""
        taken_at = self.cleaned_data.get("taken_at")
        if taken_at is None:
            return taken_at
        last = date(timezone.localdate().year + 1, 12, 31)
        try:
            day = timezone.localtime(taken_at).date()
        except (OverflowError, ValueError):
            day = None
        if day is None or not EARLIEST_STOCK_TAKE_DATE <= day <= last:
            raise forms.ValidationError(
                f"Date hors limites : entre le {EARLIEST_STOCK_TAKE_DATE:%d/%m/%Y} et le {last:%d/%m/%Y}."
            )
        return taken_at


#: A second row naming what a row above already counts. Not added up for the
#: user: the two may be counted in different units (bottles, litres).
DUPLICATE_ROW = "Déjà compté plus haut : additionnez les quantités sur une seule ligne."


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
        quantity = cleaned.get("counted_quantity")
        # Saved valued 0 EUR, it lowered the closing count of the variance and
        # the gaps. Refused when typed only: a saved line coming back
        # untouched must not trap the inventory it is in.
        if quantity is not None and quantity < 0 and "counted_quantity" in self.changed_data:
            self.add_error(None, "La quantité comptée ne peut pas être négative.")
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


class BaseStockTakeLineFormSet(BaseInlineFormSet):
    """Every row names its product with its supplier, or its stock item
    (StockTakeLineForm's `entry_search`): selected with the lines, or each
    row read them with two queries of its own - 209 of the 220 an inventory
    of 104 lines took to open. Never the stock take: a save values its lines
    as of the date it has just written (_save_stock_take_line), not the one
    read with them."""

    def __init__(self, *args, queryset=None, **kwargs):
        if queryset is None:
            queryset = StockTakeLine.objects.select_related("product__supplier", "stock_type")
        super().__init__(*args, queryset=queryset, **kwargs)

    def clean(self):
        """One line per product or article (the model's two unique
        constraints), said on the repeated row. Django checks neither: both
        fields are set by StockTakeLineForm.clean, not posted, so the save
        ended on an IntegrityError. A row being deleted is not counted."""
        super().clean()
        seen = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.errors or self._should_delete_form(form):
                continue
            line = form.instance
            key = ("product", line.product_id) if line.product_id else ("stock_type", line.stock_type_id)
            if key[1] is None:
                continue
            if key in seen:
                form.add_error("entry_search", DUPLICATE_ROW)
            seen.add(key)


StockTakeLineFormSet = inlineformset_factory(
    StockTake,
    StockTakeLine,
    form=StockTakeLineForm,
    formset=BaseStockTakeLineFormSet,
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
