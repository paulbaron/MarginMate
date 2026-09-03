import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, ListView, UpdateView

from .forms import StockTakeForm, StockTakeLineFormSet, StockTypeForm, stock_take_entry_lookup
from .models import Product, StockMovement, StockTake, StockTakeLineSource, StockType, UnitChoices
from .product_matching_rules import apply_rules_to_pending_products
from .variance import compute_variance, quantities_sold
from .services import (
    link_product_to_stock_type,
    merge_stock_types,
    product_base_amount,
    refresh_invoice_statuses_for_product,
    unlink_product,
    update_product_conversion,
    value_counted_quantity,
    value_counted_stock_type_quantity,
)


def existing_categories():
    return StockType.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")


class StockListView(ListView):
    model = StockType
    template_name = "inventory/stock_list.html"
    context_object_name = "stock_types"

    def get_queryset(self):
        return StockType.objects.all().order_by("category", "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        stock_types = list(context["stock_types"])

        # Only the per-type totals here - one query for every movement's
        # (quantity, unit_cost_ht, invoice line total/VAT) instead of a
        # separate aggregate query per stock type (was 3-4 queries x 316
        # stock types = well over a thousand). Summed in Python rather than
        # via StockType.current_quantity/current_value_ht/current_value_ttc
        # (still fine as convenience properties elsewhere, e.g. the admin
        # list view - one query per row doesn't matter there the way it
        # does with every stock type on screen at once here) - SQLite's own
        # SUM()/multiplication isn't true decimal arithmetic and drifts
        # slightly once there are enough rows, which Python's Decimal
        # doesn't.
        #
        # The purchase-history detail (2500+ rows and growing) is
        # deliberately NOT fetched here at all - seeing every stock type's
        # full history at once, most of it hidden behind a collapsed
        # section nobody opens, was most of why this page used to take
        # several seconds just to render (8+ MB of HTML). It's fetched
        # lazily per stock type instead, the first time a row is expanded -
        # see stock_type_movements() below.
        quantity_by_type: dict[int, Decimal] = {}
        value_ht_by_type: dict[int, Decimal] = {}
        value_ttc_by_type: dict[int, Decimal] = {}
        values = StockMovement.objects.values_list(
            "stock_type_id", "quantity", "unit_cost_ht", "invoice_line__total_ht", "invoice_line__vat_rate"
        )
        for stock_type_id, quantity, unit_cost_ht, line_total_ht, vat_rate in values:
            quantity_by_type[stock_type_id] = quantity_by_type.get(stock_type_id, Decimal("0")) + quantity
            value_ht_by_type[stock_type_id] = value_ht_by_type.get(stock_type_id, Decimal("0")) + (
                quantity * unit_cost_ht
            )
            if line_total_ht is not None:
                value_ttc_by_type[stock_type_id] = value_ttc_by_type.get(stock_type_id, Decimal("0")) + (
                    line_total_ht * (vat_rate + Decimal("1"))
                )

        rows = [
            {
                "stock_type": st,
                "quantity": quantity_by_type.get(st.id, Decimal("0")),
                "value_ht": value_ht_by_type.get(st.id, Decimal("0")),
                "value_ttc": value_ttc_by_type.get(st.id, Decimal("0")),
            }
            for st in stock_types
        ]
        categories = []
        for category, group in groupby(rows, key=lambda row: row["stock_type"].category):
            category_rows = list(group)
            categories.append(
                {
                    "name": category or "Sans catégorie",
                    "rows": category_rows,
                    "total_value_ht": sum((row["value_ht"] for row in category_rows), start=Decimal("0")),
                    "total_value_ttc": sum((row["value_ttc"] for row in category_rows), start=Decimal("0")),
                }
            )
        context["categories"] = categories
        context["total_value_ht"] = sum((row["value_ht"] for row in rows), start=0)
        context["total_value_ttc"] = sum((row["value_ttc"] for row in rows), start=0)
        context["stock_type_count"] = len(stock_types)
        # How much of each item has been sold - see variance.quantities_sold
        # for why it is two numbers rather than one.
        sold = quantities_sold()
        for category in categories:
            for row in category["rows"]:
                row["sold"] = sold.get(row["stock_type"].id)

        context["review_count"] = Product.objects.filter(stock_type__isnull=True).count()
        context["empty_stock_type_count"] = StockType.objects.filter(products__isnull=True).distinct().count()
        return context


def _stock_type_movement_entries(stock_type):
    movements_qs = (
        StockMovement.objects.filter(stock_type=stock_type)
        .select_related("invoice_line__invoice__supplier", "invoice_line__product")
        .order_by("-invoice_line__invoice__invoice_date", "-created_at")
    )
    entries = []
    for m in movements_qs:
        line = m.invoice_line
        entries.append(
            {
                "movement": m,
                "line": line,
                "total_ttc": line.total_ht * (1 + line.vat_rate) if line else None,
                "vat_percent": line.vat_rate * 100 if line else None,
                # Same fallback the actual stock computation uses (total_volume
                # when measured, else the item count) - showing total_volume
                # unconditionally here made "Quantité achetée" read as 0
                # whenever a line had no measured volume, even though the real
                # stock contribution was correctly computed from quantity.
                "purchased_quantity": product_base_amount(line) if line else None,
                # The unit label to print next to purchased_quantity - the
                # stock type's own unit (e.g. Kilogramme) only when the line
                # actually measured one; a plain item count (a jar, a can)
                # is always "Unité" regardless of what unit the stock type
                # tracks in, since product.unit now always mirrors the stock
                # type (see assign_product) and would otherwise mislabel
                # e.g. "2" jars of tahina as "2 Kilogramme".
                "purchased_quantity_unit": (
                    stock_type.get_unit_display()
                    if line and line.product.unit != UnitChoices.UNIT and line.total_volume
                    else "Unité"
                ),
                # Clean display value for the inline edit input - a raw
                # Decimal shows as "0.7000", not "0.7".
                "stock_equivalent_display": (f"{float(line.product.stock_equivalent):g}" if line else None),
            }
        )
    return entries


def _build_price_history_svg(points: list[tuple]) -> str:
    """points: [(date, unit_cost_ht), ...] oldest first, as an inline SVG line
    chart.

    Still hand-rolled rather than pulling in a charting library. Chart.js and
    friends are ~65KB gzipped plus a CDN dependency, and would replace a
    server-rendered SVG - which works with JavaScript off and prints - with a
    canvas that doesn't. For one line of a few dozen points and one pie of at
    most eight slices, the only thing they'd buy is the hover tooltip, and
    that's static/js/charts.js: about eighty lines, no dependency.

    The markup exists to be enhanced: every point carries its date and price
    in data attributes, so the tooltip shows real values rather than the
    browser's own sluggish <title> tooltip.
    """
    if len(points) < 2:
        return ""

    width, height = 640, 220
    pad_left, pad_right, pad_top, pad_bottom = 55, 20, 20, 30
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    dates = [p[0] for p in points]
    prices = [float(p[1]) for p in points]
    min_price, max_price = min(prices), max(prices)
    if min_price == max_price:
        min_price -= 1
        max_price += 1
    date_min, date_max = dates[0], dates[-1]
    date_span = (date_max - date_min).days or 1

    def x_for(date):
        return pad_left + (date - date_min).days / date_span * plot_w

    def y_for(price):
        return pad_top + (1 - (price - min_price) / (max_price - min_price)) * plot_h

    coords = list(zip((x_for(d) for d in dates), (y_for(p) for p in prices)))
    polyline_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    dots = "".join(
        f'<circle class="chart-point" cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--amber)" '
        f'data-x="{x:.1f}" data-y="{y:.1f}" data-label="{d:%d/%m/%Y}" data-value="{p:.4f} €" />'
        for (x, y), d, p in zip(coords, dates, prices)
    )
    # A faint fill under the line makes the shape readable at a glance, which
    # is what this chart is actually for - "is it getting dearer".
    area = (
        f'<polygon points="{pad_left:.1f},{height - pad_bottom} {polyline_points} '
        f'{width - pad_right:.1f},{height - pad_bottom}" fill="var(--amber)" opacity="0.08" />'
    )

    return (
        f'<div class="chart" data-chart="line" data-plot="{pad_left},{pad_top},{plot_w},{plot_h}">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Évolution du prix unitaire">'
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{height - pad_bottom}" '
        f'stroke="var(--border)" />'
        f'<line x1="{pad_left}" y1="{height - pad_bottom}" x2="{width - pad_right}" y2="{height - pad_bottom}" '
        f'stroke="var(--border)" />'
        f'<text x="4" y="{pad_top + 4}" font-size="11" fill="var(--muted)">{max_price:.2f} €</text>'
        f'<text x="4" y="{height - pad_bottom}" font-size="11" fill="var(--muted)">{min_price:.2f} €</text>'
        f'<text x="{pad_left}" y="{height - 8}" font-size="11" fill="var(--muted)">{date_min:%d/%m/%Y}</text>'
        f'<text x="{width - pad_right}" y="{height - 8}" font-size="11" fill="var(--muted)" '
        f'text-anchor="end">{date_max:%d/%m/%Y}</text>'
        f"{area}"
        f'<polyline points="{polyline_points}" fill="none" stroke="var(--amber)" stroke-width="2" />'
        f'<line class="chart-hover-line" x1="0" y1="{pad_top}" x2="0" y2="{height - pad_bottom}" />'
        f'<circle class="chart-hover-dot" cx="0" cy="0" r="5" />'
        f"{dots}"
        f"</svg>"
        f'<div class="chart-tooltip" data-chart-tooltip></div>'
        f"</div>"
    )


def stock_type_price_history(request, pk):
    """Lazy-loaded (see stock_type_movements below) price-over-time chart
    for one stock type, plotting every movement's unit_cost_ht against its
    invoice date."""
    stock_type = get_object_or_404(StockType, pk=pk)
    points = list(
        StockMovement.objects.filter(stock_type=stock_type, invoice_line__isnull=False)
        .order_by("invoice_line__invoice__invoice_date")
        .values_list("invoice_line__invoice__invoice_date", "unit_cost_ht")
    )
    return render(
        request,
        "inventory/_stock_type_price_history.html",
        {"stock_type": stock_type, "chart_svg": _build_price_history_svg(points), "has_enough_data": len(points) >= 2},
    )


def stock_type_movements(request, pk):
    """Purchase history for one stock type - fetched on demand (see
    StockListView.get_context_data for why this isn't just baked into the
    main page for every stock type up front) the first time its row is
    expanded, via a plain hx-get/htmx.ajax call from stock_list.html."""
    stock_type = get_object_or_404(StockType, pk=pk)
    return render(
        request,
        "inventory/_stock_type_movements.html",
        {
            "stock_type": stock_type,
            "movements": _stock_type_movement_entries(stock_type),
        },
    )


def search_stock_types(request):
    """Backs the Stock page's search box: matches a stock type by its own
    name/category, or by the raw_name of any product filed under it, so
    typing what's actually printed on an invoice still finds the right row
    even when the stock type itself was named something more generic."""
    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse({"ids": []})
    ids = (
        StockType.objects.filter(
            Q(name__icontains=query) | Q(category__icontains=query) | Q(products__raw_name__icontains=query)
        )
        .distinct()
        .values_list("id", flat=True)
    )
    return JsonResponse({"ids": list(ids)})


def export_associations(request):
    """Downloadable snapshot of every product's stock-item classification -
    a backup of the (often manual, time-consuming) review work, and a way
    to seed another instance with it instead of starting from zero. Matched
    back up on import by (supplier name, raw invoice name), the same pair
    Product itself is uniquely keyed on."""
    products = Product.objects.filter(stock_type__isnull=False).select_related("stock_type", "supplier")
    data = [
        {
            "supplier": product.supplier.name,
            "raw_name": product.raw_name,
            "product_unit": product.unit,
            "stock_equivalent": str(product.stock_equivalent),
            "stock_type_name": product.stock_type.name,
            "stock_type_category": product.stock_type.category,
            "stock_type_unit": product.stock_type.unit,
        }
        for product in products
    ]
    payload = json.dumps({"version": 1, "products": data}, ensure_ascii=False, indent=2)
    response = HttpResponse(payload, content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="marginmate-associations.json"'
    return response


def import_associations(request):
    """Replays an export_associations file against this instance. Never
    overwrites an existing classification - a product already linked to a
    different stock type than the file says is skipped and counted, not
    silently changed, so importing someone else's work can't clobber your
    own review decisions."""
    if request.method != "POST":
        return render(request, "inventory/import_associations.html")

    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Choisissez un fichier à importer.")
        return redirect("inventory:import_associations")

    try:
        payload = json.loads(upload.read().decode("utf-8"))
        entries = payload["products"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        messages.error(request, "Fichier invalide ou mal formé.")
        return redirect("inventory:import_associations")

    applied = 0
    skipped_conflict = 0
    skipped_unmatched = 0
    for entry in entries:
        try:
            supplier_name = entry["supplier"]
            raw_name = entry["raw_name"]
            stock_type_name = (entry["stock_type_name"] or "").strip()
        except (KeyError, TypeError):
            skipped_unmatched += 1
            continue
        if not stock_type_name:
            skipped_unmatched += 1
            continue

        product = Product.objects.filter(supplier__name=supplier_name, raw_name=raw_name).first()
        if product is None:
            skipped_unmatched += 1
            continue

        if product.stock_type_id is not None:
            already_matches = product.stock_type.name.lower() == stock_type_name.lower()
            if already_matches:
                applied += 1
            else:
                skipped_conflict += 1
            continue

        stock_type = StockType.objects.filter(name__iexact=stock_type_name).first()
        if stock_type is None:
            stock_type = StockType.objects.create(
                name=stock_type_name,
                category=(entry.get("stock_type_category") or "").strip(),
                unit=entry.get("stock_type_unit") or UnitChoices.UNIT,
            )
        stock_equivalent = _parse_positive_decimal(str(entry.get("stock_equivalent", "1")), default=Decimal("1"))
        link_product_to_stock_type(product, stock_type, unit=stock_type.unit, stock_equivalent=stock_equivalent or Decimal("1"))
        applied += 1

    messages.success(
        request,
        f"{applied} association(s) appliquée(s), {skipped_conflict} ignorée(s) (déjà classé différemment), "
        f"{skipped_unmatched} ignorée(s) (produit introuvable dans cette instance).",
    )
    return redirect("inventory:stock_list")


class CategoryAutocompleteMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["existing_categories"] = existing_categories()
        return context


class StockTypeCreateView(CategoryAutocompleteMixin, CreateView):
    model = StockType
    form_class = StockTypeForm
    template_name = "inventory/stock_type_form.html"
    success_url = reverse_lazy("inventory:stock_list")


class StockTypeUpdateView(CategoryAutocompleteMixin, UpdateView):
    model = StockType
    form_class = StockTypeForm
    template_name = "inventory/stock_type_form.html"
    success_url = reverse_lazy("inventory:stock_list")

    def form_valid(self, form):
        # product.unit always mirrors its stock type's unit (see
        # assign_product) - but nothing keeps that true automatically if the
        # stock type's OWN unit is edited after products are already linked,
        # so every linked product's unit/movements get recomputed here too.
        # Without this, changing "Prosecco" from Unité to Litre would save
        # the new unit but leave every purchase still counted as bottles.
        old_unit = StockType.objects.get(pk=self.object.pk).unit
        response = super().form_valid(form)
        if self.object.unit != old_unit:
            products = list(self.object.products.all())
            for product in products:
                update_product_conversion(product, unit=self.object.unit, stock_equivalent=product.stock_equivalent)
            if products:
                messages.info(
                    self.request,
                    f"Unité changée : {len(products)} produit(s) lié(s) à \"{self.object.name}\" "
                    "recalculé(s) en conséquence.",
                )
        return response

    def form_invalid(self, form):
        # A rename that collides with a different existing stock type is
        # offered as a merge instead of just being rejected outright - very
        # often that collision IS exactly two brand-specific duplicates of
        # the same real thing (e.g. renaming "Gin Biillyon" to "Gin") that
        # should have been one stock type all along. Detected independently
        # of whatever Django's own validation message says (never string-
        # match error text - it's in English here regardless of the rest of
        # the app per LANGUAGE_CODE, and could change between versions).
        new_name = (form.data.get("name") or "").strip()
        conflict = None
        if new_name and form.errors.get("name"):
            conflict = StockType.objects.filter(name__iexact=new_name).exclude(pk=self.object.pk).first()
        context = self.get_context_data(form=form)
        if conflict:
            context["merge_candidate"] = conflict
            # ModelForm._post_clean() mutates self.object's fields in place
            # to the submitted values during validation, even though nothing
            # gets saved on an invalid form - re-fetch so the merge prompt
            # shows what's actually in the database, not the rejected edit.
            context["current_stock_type"] = StockType.objects.get(pk=self.object.pk)
        return self.render_to_response(context)


def merge_stock_type(request, pk):
    """Confirmed from the "this name already exists" prompt on the edit
    form (see StockTypeUpdateView.form_invalid) - merges `pk` into whatever
    stock type `target_id` names, deleting `pk`. Any other field changes
    that were being made on `pk` (category, unit) are discarded along with
    it: choosing to merge means "this is the same thing", not "also apply
    my edits to the survivor"."""
    if request.method != "POST":
        return redirect("inventory:stock_list")
    source = get_object_or_404(StockType, pk=pk)
    target = get_object_or_404(StockType, pk=request.POST.get("target_id"))
    if source.unit != target.unit:
        messages.error(
            request,
            f'Fusion impossible : "{source.name}" est en {source.get_unit_display()}, '
            f'"{target.name}" est en {target.get_unit_display()}. Changez d\'abord l\'unité '
            "de l'un des deux, ou déplacez les produits manuellement.",
        )
        return redirect("inventory:stock_type_update", pk=source.pk)
    source_name, target_name = source.name, target.name
    merge_stock_types(source, target)
    messages.success(request, f'"{source_name}" fusionné dans "{target_name}".')
    return redirect("inventory:stock_list")


def delete_stock_type(request, pk):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    stock_type = get_object_or_404(StockType, pk=pk)
    name = stock_type.name
    affected_products = list(stock_type.products.all())
    # Product.stock_type is SET_NULL and StockMovement.stock_type is CASCADE,
    # so this alone sends every associated product back to the review queue
    # and drops the stock type's ledger entries. That cascade happens at the
    # DB level, bypassing unlink_product() - so invoice statuses need
    # refreshing separately here, or their invoices would stay marked
    # COMPLETE despite now containing an unreviewed product again.
    stock_type.delete()
    for product in affected_products:
        refresh_invoice_statuses_for_product(product)
    messages.success(request, f'Type de stock "{name}" supprimé. Les produits associés sont repassés en vérification.')
    return redirect("inventory:stock_list")


def clear_empty_stock_types(request):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    empty = StockType.objects.filter(products__isnull=True).distinct()
    count = empty.count()
    empty.delete()
    if count:
        messages.success(request, f"{count} type(s) de stock vide(s) supprimé(s).")
    else:
        messages.info(request, "Aucun type de stock vide à supprimer.")
    return redirect("inventory:stock_list")


class ReviewQueueView(ListView):
    model = Product
    template_name = "inventory/review_queue.html"
    context_object_name = "products"
    paginate_by = 50

    def get_queryset(self):
        # Every pending product gets a suggestion before the page ever
        # renders - no separate button to click, no stale/blank rows. Cheap
        # to call on every visit: it only touches products with no
        # suggestion yet, so once the queue is fully autofilled this is a
        # single no-op query.
        apply_rules_to_pending_products()
        return (
            Product.objects.filter(stock_type__isnull=True)
            .select_related("supplier")
            # Without the `__invoice` half, `{{ line.invoice.invoice_date }}`
            # in the template hits the DB once per invoice line instead of
            # once total - the single biggest cost on this page (2439 of
            # 2445 queries, ~5s, before this fix).
            .prefetch_related("invoice_lines__invoice")
            .order_by("raw_name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stock_types"] = StockType.objects.all()
        context["unit_choices"] = UnitChoices.choices
        context["existing_categories"] = existing_categories()

        # Deliberately NOT derived from context["products"]: that's just the
        # current page post-pagination, but "Approuver toutes les
        # suggestions" and the intro text both talk about the whole queue -
        # only the JSON blob is fetched (not full rows) since that's all
        # this needs.
        all_suggestions = list(
            Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=False).values_list(
                "ai_suggestion", flat=True
            )
        )
        context["suggested_count"] = len(all_suggestions)
        context["confidence_counts"] = Counter(s.get("confidence", "?") for s in all_suggestions)
        context["fallback_count"] = sum(1 for s in all_suggestions if s.get("source") == "fallback")
        return context


def remove_product(request, product_id):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    unlink_product(product)
    messages.success(request, f'"{product.raw_name}" retiré du stock et repassé en vérification.')
    return redirect("inventory:stock_list")


def edit_product_conversion(request, product_id):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=None)
    if stock_equivalent is None:
        messages.error(request, "Facteur invalide.")
        return redirect("inventory:stock_list")
    # product.unit always mirrors its stock type's unit now (see
    # assign_product) - there's nothing left for a human to choose here
    # beyond the conversion factor itself.
    update_product_conversion(product, unit=product.stock_type.unit, stock_equivalent=stock_equivalent)
    messages.success(request, f'"{product.raw_name}" mis à jour (facteur {stock_equivalent}, {product.stock_type}).')
    return redirect("inventory:stock_list")


def _resolve_suggestion_stock_type(suggestion: dict) -> StockType | None:
    matched_id = suggestion.get("matched_stock_type_id")
    if matched_id:
        stock_type = StockType.objects.filter(pk=matched_id).first()
        if stock_type:
            return stock_type
    name = (suggestion.get("stock_type_name") or "").strip()
    if not name:
        return None
    unit = suggestion.get("new_stock_type_unit") or UnitChoices.UNIT
    category = (suggestion.get("new_stock_type_category") or "").strip()
    stock_type, _created = StockType.objects.get_or_create(
        name__iexact=name,
        defaults={"name": name, "unit": unit, "category": category},
    )
    return stock_type


def approve_all_suggestions(request):
    if request.method != "POST":
        return redirect("inventory:review_queue")

    products = Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=False)
    approved = 0
    skip_reasons = Counter()
    for product in products:
        suggestion = product.ai_suggestion
        stock_equivalent = _parse_positive_decimal(str(suggestion.get("stock_equivalent", "")), default=None)
        stock_type = _resolve_suggestion_stock_type(suggestion)

        reason = None
        if stock_type is None:
            reason = "aucun type de stock identifié"
        elif stock_equivalent is None:
            reason = f"facteur de conversion invalide ({suggestion.get('stock_equivalent')!r})"

        if reason:
            skip_reasons[reason] += 1
            # Clear it so the next visit to the review queue re-generates a
            # suggestion for it (ReviewQueueView.get_queryset calls
            # apply_rules_to_pending_products on every request, which only
            # ever touches products with no suggestion yet) instead of it
            # being permanently stuck with a bad one.
            product.ai_suggestion = None
            product.save(update_fields=["ai_suggestion"])
            continue

        # product.unit always mirrors stock_type.unit - see assign_product.
        link_product_to_stock_type(product, stock_type, unit=stock_type.unit, stock_equivalent=stock_equivalent)
        approved += 1

    skipped = sum(skip_reasons.values())
    if skipped:
        detail = ", ".join(f"{count} ({reason})" for reason, count in skip_reasons.most_common())
        messages.warning(
            request,
            f"{approved} produit(s) rattaché(s) d'après les suggestions. {skipped} ignoré(s) : {detail}. "
            "Ces produits sont repassés sans suggestion - une nouvelle sera générée automatiquement.",
        )
    elif approved:
        messages.success(request, f"{approved} produit(s) rattaché(s) automatiquement d'après les suggestions.")
    else:
        messages.info(request, "Aucune suggestion à approuver pour le moment.")
    return redirect("inventory:review_queue")


def _parse_positive_decimal(raw: str, default: Decimal) -> Decimal | None:
    """Returns the parsed value, `default` if blank, or None if invalid."""
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def assign_product(request, product_id):
    if request.method != "POST":
        return redirect("inventory:review_queue")

    product = get_object_or_404(Product, pk=product_id)
    name = request.POST.get("stock_type_name", "").strip()
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=Decimal("1"))

    if stock_equivalent is None:
        messages.error(request, "L'équivalence en stock doit être un nombre positif.")
        return redirect("inventory:review_queue")

    if not name:
        messages.error(request, "Donnez un nom de type de stock.")
        return redirect("inventory:review_queue")

    # A name matching an existing type (case-insensitively) is used as-is -
    # the unit/category fields only matter for creating a brand new one, the
    # same resolution _resolve_suggestion_stock_type already does for
    # suggestions.
    stock_type = StockType.objects.filter(name__iexact=name).first()
    if stock_type is None:
        unit = request.POST.get("new_stock_type_unit") or UnitChoices.UNIT
        category = request.POST.get("new_stock_type_category", "").strip()
        stock_type = StockType.objects.create(name=name, unit=unit, category=category)

    # product.unit isn't a separate human choice: it always mirrors whatever
    # stock type ends up being used (existing types keep their own unit
    # regardless of what the "new type" dropdown said, via get_or_create's
    # defaults being ignored when a match already exists) - see
    # product_base_amount in services.py for why "Litre" vs "Kilogramme"
    # never actually changes anything, only "Unité" vs. not does.
    link_product_to_stock_type(product, stock_type, unit=stock_type.unit, stock_equivalent=stock_equivalent)
    messages.success(request, f'"{product.raw_name}" lié à "{stock_type.name}".')
    return redirect("inventory:review_queue")


def stock_take_variance(request, pk):
    """Where did the stock go? See inventory/variance.py for the reasoning -
    in short: what physically left the shelf, minus what the sales explain,
    with a recipe's alternatives pooled rather than guessed at."""
    stock_take = get_object_or_404(StockTake, pk=pk)
    return render(
        request,
        "inventory/stock_take_variance.html",
        {"report": compute_variance(stock_take)},
    )


class StockTakeListView(ListView):
    model = StockTake
    template_name = "inventory/stock_take_list.html"
    context_object_name = "stock_takes"


def _save_stock_take_line(line):
    """Value one changed/new stock-take line and replace its source
    breakdown - called only for lines formset.save(commit=False) actually
    returns (new lines, and existing ones the user changed), so an
    untouched existing line keeps reporting exactly what it did when it
    was last saved (see StockTake's docstring on why that's frozen)."""
    # A count is priced from the purchases that had actually arrived by the
    # day it was taken - back-dating a stock take must not reach forward
    # into later deliveries. See services._purchase_ladder.
    as_of = timezone.localtime(line.stock_take.taken_at).date()
    if line.product_id:
        result = value_counted_quantity(line.product, line.counted_quantity, line.unit, as_of=as_of)
    else:
        result = value_counted_stock_type_quantity(line.stock_type, line.counted_quantity, as_of=as_of)
    line.value_ht = result["value_ht"]
    line.has_shortfall = result["has_shortfall"]
    line.shortfall_quantity = result["shortfall_quantity"]
    line.save()
    line.sources.all().delete()
    StockTakeLineSource.objects.bulk_create(
        StockTakeLineSource(
            stock_take_line=line,
            invoice_line=source["invoice_line"],
            quantity_used=source["quantity_used"],
            unit_cost_ht=source["unit_cost_ht"],
        )
        for source in result["sources"]
    )


def _stock_take_form_view(request, stock_take):
    if request.method == "POST":
        form = StockTakeForm(request.POST, instance=stock_take)
        formset = StockTakeLineFormSet(request.POST, instance=stock_take)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                stock_take = form.save()
                lines = formset.save(commit=False)
                for line in lines:
                    _save_stock_take_line(line)
                for obj in formset.deleted_objects:
                    obj.delete()
            messages.success(request, "Inventaire enregistré.")
            return redirect("inventory:stock_take_detail", pk=stock_take.pk)
    else:
        initial = {} if stock_take.pk else {"taken_at": timezone.now()}
        form = StockTakeForm(instance=stock_take, initial=initial)
        formset = StockTakeLineFormSet(instance=stock_take)
    entries = stock_take_entry_lookup()
    return render(
        request,
        "inventory/stock_take_form.html",
        {
            "stock_take": stock_take,
            "form": form,
            "formset": formset,
            "entry_names": entries.keys(),
            "entry_data": json.dumps(entries),
        },
    )


def stock_take_create(request):
    return _stock_take_form_view(request, StockTake())


def stock_take_update(request, pk):
    return _stock_take_form_view(request, get_object_or_404(StockTake, pk=pk))


def stock_take_detail(request, pk):
    stock_take = get_object_or_404(StockTake, pk=pk)
    lines = list(
        stock_take.lines.select_related("product", "product__supplier", "stock_type")
        .prefetch_related("sources__invoice_line__invoice")
        .order_by("product__raw_name", "stock_type__name")
    )
    return render(
        request,
        "inventory/stock_take_detail.html",
        {
            "stock_take": stock_take,
            "lines": lines,
            # Counted from the rows already fetched - StockTake.has_shortfall
            # would run its own query, and the page wants the number anyway.
            "shortfall_count": sum(1 for line in lines if line.has_shortfall),
        },
    )


def stock_take_delete(request, pk):
    if request.method != "POST":
        return redirect("inventory:stock_take_list")
    stock_take = get_object_or_404(StockTake, pk=pk)
    stock_take.delete()
    messages.success(request, "Inventaire supprimé.")
    return redirect("inventory:stock_take_list")
