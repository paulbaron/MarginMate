import json
import unicodedata
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.db.models import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from common import is_id

from .forms import (
    OLD_STOCK_TYPE_ENTRY_SUFFIXES,
    STOCK_TYPE_ENTRY_SUFFIX,
    EntryResolver,
    StockTakeForm,
    StockTakeLineFormSet,
    StockTypeForm,
    is_stock_type_entry,
    stock_take_entry_lookup,
)
from .models import MovementKind, Product, StockMovement, StockTake, StockTakeLine, StockTakeLineSource, StockType, UnitChoices
from .product_matching_rules import apply_rules_to_pending_products
from .variance import (
    PeriodStock,
    SoldQuantity,
    StockPeriod,
    compute_variance,
    quantities_sold,
    stock_between,
)
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


class StockListView(TemplateView):
    """"Produits & charges": every stock item and what was spent on it, the
    charges under them - and, beside them, the products no stock item has
    claimed yet.

    One page because it is one job: a product is classified in the side
    panel and checked in the list next to it, where it has just landed (the
    panel's script reloads the list, `stock_catalogue`, opened on that
    item). The review queue used to be a page of its own, and checking a
    classification meant going back and forth between the two.
    """

    template_name = "inventory/stock_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(catalogue_context(self.request))
        context.update(review_panel_context())
        return context


def stock_catalogue(request):
    """The list alone, and its headline figures, for the page to reload in
    place after a product was classified or sent back."""
    return render(request, "inventory/_catalogue_refresh.html", catalogue_context(request))


def catalogue_context(request) -> dict:
    """The list: categories of stock items with what was bought and sold over
    all time, or what moved over the period `?inventaire=` names."""
    stock_types = list(StockType.objects.all().order_by("category", "name"))
    context = {}

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
    #
    # Two sets of sums from the one scan. What the page shows all time -
    # "Acheté", "Total HT/TTC", "Total acheté" - is what was BOUGHT: the
    # purchases alone, so a loss written down later never makes "acheté" a
    # lie. The ledger (every movement: bought, less the losses and
    # corrections) is the stock one: it is how much could have been poured,
    # the ceiling "Vendu" respects, and it prices an item the way
    # StockType.current_unit_cost_ht does. The two are the same numbers until
    # somebody records a loss.
    quantity_by_type: dict[int, Decimal] = {}
    value_ht_by_type: dict[int, Decimal] = {}
    bought_by_type: dict[int, Decimal] = {}
    bought_ht_by_type: dict[int, Decimal] = {}
    value_ttc_by_type: dict[int, Decimal] = {}
    values = StockMovement.objects.values_list(
        "stock_type_id", "kind", "quantity", "unit_cost_ht", "invoice_line__total_ht", "invoice_line__vat_rate"
    )
    for stock_type_id, kind, quantity, unit_cost_ht, line_total_ht, vat_rate in values:
        quantity_by_type[stock_type_id] = quantity_by_type.get(stock_type_id, Decimal("0")) + quantity
        value_ht_by_type[stock_type_id] = value_ht_by_type.get(stock_type_id, Decimal("0")) + (
            quantity * unit_cost_ht
        )
        if kind != MovementKind.PURCHASE:
            continue
        bought_by_type[stock_type_id] = bought_by_type.get(stock_type_id, Decimal("0")) + quantity
        bought_ht_by_type[stock_type_id] = bought_ht_by_type.get(stock_type_id, Decimal("0")) + (
            quantity * unit_cost_ht
        )
        if line_total_ht is not None:
            value_ttc_by_type[stock_type_id] = value_ttc_by_type.get(stock_type_id, Decimal("0")) + (
                line_total_ht * (vat_rate + Decimal("1"))
            )

    rows = [
        {
            "stock_type": st,
            "quantity": bought_by_type.get(st.id, Decimal("0")),
            "value_ht": bought_ht_by_type.get(st.id, Decimal("0")),
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
    # for why it is two numbers rather than one. unit_costs reuses the
    # sums already computed above (same formula as
    # StockType.current_unit_cost_ht) rather than have quantities_sold()
    # scan StockMovement a second time for the same numbers.
    unit_costs = {
        stock_type_id: value_ht_by_type[stock_type_id] / quantity
        for stock_type_id, quantity in quantity_by_type.items()
        if quantity
    }
    period = selected_period(request)
    context["period"] = period
    context["stock_takes"] = StockTake.objects.all()
    if period is None:
        # All time. The ledger quantities are the ceiling: what was bought
        # (the "Acheté" column beside "Vendu"), less the losses recorded,
        # which cannot have been sold.
        sold = quantities_sold(unit_costs=unit_costs, available=quantity_by_type)
    else:
        sold = quantities_sold(
            period.start, period.end, unit_costs=unit_costs, available=period.ceilings()
        )
    _attach_sold(context, categories, sold, period, unit_costs)

    context["review_count"] = Product.objects.filter(stock_type__isnull=True, is_expense=False).count()
    context["empty_stock_type_count"] = StockType.objects.filter(products__isnull=True).distinct().count()
    context["charge_suppliers"] = charge_suppliers(period)
    context["charge_total_ttc"] = sum(
        (row["total_ttc"] for row in context["charge_suppliers"]), Decimal("0")
    )
    return context


def charge_suppliers(period=None) -> list[dict]:
    """What the suppliers of charges cost - a subscription, the rent, the
    water. They hold no stock, so they are nowhere else on this page; they
    are spending all the same, and seeing it beside the purchases is the
    whole point of showing them here.

    Over the window being looked at, or the last twelve months by default:
    an all-time total of a monthly subscription says little.

    **Every supplier is a row that opens**, on its documents and on its
    curve, exactly as a stock item does - the water bill has one poste and
    the rent statement six, and a page that only opened the second left
    five suppliers out of six with nothing to click. Where a document names
    several postes, those are rows of their own underneath (the rent apart
    from the building provision, which is the one that gets regularised -
    see invoices.charges); where it names one, the supplier's row *is* that
    poste and repeating it below would say the same thing twice.

    A supplier that billed nothing over the window is listed all the same,
    with the date of its last document: a water bill arriving twice a year
    would otherwise drop off the page between two of them, taking its whole
    history with it.
    """
    from invoices.models import Invoice, InvoiceLine, Supplier

    suppliers = list(Supplier.objects.filter(expenses_only=True).order_by("name"))
    if not suppliers:
        return []
    every_document = Invoice.objects.filter(supplier__in=suppliers)
    documents = every_document.exclude(invoice_date=None)
    if period is not None:
        # The first stock take's window has no start: it runs from the
        # beginning, and a None in the filter was a 500.
        documents = documents.filter(invoice_date__lte=period.end)
        if period.start is not None:
            documents = documents.filter(invoice_date__gte=period.start)
        since = period.start
    else:
        since = timezone.localdate() - timedelta(days=365)
        documents = documents.filter(invoice_date__gte=since)
    rows: dict[int, dict] = {
        supplier.pk: {
            "supplier": supplier, "documents": 0, "total_ttc": Decimal("0"), "last": None, "postes": {}
        }
        for supplier in suppliers
    }
    for line in InvoiceLine.objects.filter(invoice__in=documents).select_related("invoice", "product"):
        row = rows[line.invoice.supplier_id]
        # The line's own amount, which for a charge is the figure the
        # document prints (InvoiceLine.printed_ttc): 33,33 € HT at 20% works
        # back out to 40,00 € where the bill says 39,99 €.
        amount = line.total_ttc.quantize(Decimal("0.01"))
        row["total_ttc"] += amount
        poste = row["postes"].setdefault(
            line.raw_name,
            {"name": line.raw_name, "product": line.product, "total_ttc": Decimal("0"), "invoices": set()},
        )
        poste["total_ttc"] += amount
        poste["invoices"].add(line.invoice_id)
    for invoice in documents.only("supplier_id", "invoice_date"):
        rows[invoice.supplier_id]["documents"] += 1
    # The last document ever, inside the window or not: it is what says a
    # supplier has gone quiet, and on a row showing nothing over the window
    # it is the only thing left to say.
    for invoice in every_document.exclude(invoice_date=None).only("supplier_id", "invoice_date"):
        row = rows[invoice.supplier_id]
        if row["last"] is None or invoice.invoice_date > row["last"]:
            row["last"] = invoice.invoice_date
    filed = set(every_document.values_list("supplier_id", flat=True).distinct())
    return [
        row
        | {
            "since": since,
            # One poste is the charge itself under another name, and the
            # supplier's own row already opens on it.
            "postes": sorted(
                (
                    poste | {"documents": len(poste["invoices"])}
                    for poste in row["postes"].values()
                    # A line nobody attached to a product cannot be opened;
                    # it is still counted in the supplier's total above.
                    if poste["product"] is not None
                ),
                key=lambda poste: -poste["total_ttc"],
            )
            if len(row["postes"]) > 1
            else [],
        }
        for row in rows.values()
        if row["supplier"].pk in filed
    ]


def selected_period(request) -> StockPeriod | None:
    """The window `?inventaire=<pk>` asks for, or None for all time.

    Only the CLOSING count is named; the opening one is whichever came
    before it, exactly as on the écarts page - naming both would let the
    two pages disagree about what "this period" means, and there is no
    second thing to choose anyway.
    """
    take_id = request.GET.get("inventaire")
    # Anything that isn't an id we have falls back to all time rather
    # than erroring: this is a query parameter, so a stale bookmark, a
    # since-deleted inventory or a hand-typed URL all end up here, and
    # `pk="tomorrow"` raises ValueError rather than simply not matching.
    if not is_id(take_id or ""):
        return None
    closing_take = StockTake.objects.filter(pk=take_id).first()
    return stock_between(closing_take) if closing_take is not None else None

def _attach_sold(context, categories, sold, period, unit_costs):
    """Hang the sold/period figures on each row, and total them up."""
    over_stock = 0
    uncounted = 0
    missing_value = Decimal("0")
    for category in categories:
        category["total_missing_value"] = Decimal("0")
        for row in category["rows"]:
            stock_type_id = row["stock_type"].id
            row["sold"] = sold.get(stock_type_id)
            if period is None:
                row["flag_over"] = row["sold"] is not None and row["sold"].is_over
                if row["flag_over"]:
                    over_stock += 1
                continue
            item = period.items.get(stock_type_id) or PeriodStock()
            row["period"] = item
            if row["sold"] is None:
                # Bought and counted but never sold - which is not the
                # same as "not in this period", and the difference is the
                # whole of what left the shelf being unexplained.
                row["sold"] = SoldQuantity(available=item.sellable)
            # Nothing bought, counted or sold: this item simply wasn't
            # part of the period, and there are hundreds of those.
            row["in_period"] = item.has_activity or bool(row["sold"].headline)
            # An item missing from either count has no measured opening
            # or closing, so everything it bought reads as evaporated -
            # the single biggest source of false "missing" there is (see
            # CLAUDE.md). It is listed, and left without a verdict.
            row["reliable"] = item.counted
            row["flag_over"] = False
            if not row["in_period"]:
                continue
            if not row["reliable"]:
                uncounted += 1
                continue
            row["flag_over"] = row["sold"].is_over
            if row["flag_over"]:
                over_stock += 1
            row["missing_value_ht"] = row["sold"].unexplained * unit_costs.get(stock_type_id, Decimal("0"))
            category["total_missing_value"] += row["missing_value_ht"]
            missing_value += row["missing_value_ht"]
    context["over_stock_count"] = over_stock
    context["uncounted_count"] = uncounted
    context["total_missing_value"] = missing_value
    context["column_count"] = 7 if period is None else 10


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
                "total_ttc": line.total_ttc if line else None,
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


def _aggregate_price_points(movements: list[tuple]) -> list[tuple]:
    """movements: [(date, quantity, unit_cost_ht), ...], any order - one
    (date, unit_cost_ht) point per distinct date, quantity-weighted.

    The chart plots price against date, so its x-axis needs at least two
    DISTINCT dates - not just two movements. A stock type bought twice on
    the same day (a split delivery, a same-day correction) used to slip
    past the "enough history" gate with two same-date points, which then
    collapsed the whole chart onto one x-coordinate: `date_span` came out
    as 0, so every point landed at the left edge instead of "not enough
    history" - which is what actually happened to "Bière triple".

    Weighted by how much was bought at each price rather than a plain mean,
    so a same-day 2-for-1 correction doesn't count for as much as the
    delivery it's correcting. Weighted by magnitude (not signed quantity),
    since a return still reports a real price and a negative weight would
    only cancel the movement it's correcting rather than being ignored.
    """
    grouped: dict = {}
    for occurred_on, quantity, unit_cost_ht in movements:
        grouped.setdefault(occurred_on, []).append((quantity, unit_cost_ht))

    points = []
    for occurred_on in sorted(grouped):
        entries = grouped[occurred_on]
        weight = sum(abs(quantity) for quantity, _ in entries)
        if weight > 0:
            price = sum(abs(quantity) * unit_cost_ht for quantity, unit_cost_ht in entries) / weight
        else:
            # Every movement that day nets to zero weight (e.g. a purchase
            # reversed same-day) - nothing to weight by, so just average
            # the raw prices rather than divide by zero.
            price = sum(unit_cost_ht for _, unit_cost_ht in entries) / len(entries)
        points.append((occurred_on, price))
    return points


def _build_price_history_svg(points: list[tuple], label: str = "Évolution du prix unitaire") -> str:
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
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
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
    raw_movements = StockMovement.objects.filter(
        stock_type=stock_type, invoice_line__isnull=False
    ).values_list("invoice_line__invoice__invoice_date", "quantity", "unit_cost_ht")
    points = _aggregate_price_points(raw_movements)
    return render(
        request,
        "inventory/_stock_type_price_history.html",
        {"stock_type": stock_type, "chart_svg": _build_price_history_svg(points), "has_enough_data": len(points) >= 2},
    )


def _charge_document_rows(lines, invoices=()) -> list[dict]:
    """One row per document, from the lines given - one poste's, or a whole
    supplier's.

    A bill printing two rates is read as two lines (see
    invoices.charge_reading), and a person opening a charge is counting
    documents, not rates: 29 Total Energie bills showed up as 46. So the
    tax is an amount here and never a rate - on a document carrying two of
    them, naming one would be a lie about the other.
    """
    # A document filed with nothing read still counts on its row, and is
    # the one that most needs its "Corriger": it is listed, at 0,00.
    by_document: dict[int, dict] = {
        invoice.pk: {"invoice": invoice, "total_ht": Decimal("0"), "total_ttc": Decimal("0")} for invoice in invoices
    }
    for line in lines:
        row = by_document.setdefault(
            line.invoice_id,
            {"invoice": line.invoice, "total_ht": Decimal("0"), "total_ttc": Decimal("0")},
        )
        row["total_ht"] += line.total_ht
        row["total_ttc"] += line.total_ttc
    ordered = sorted(
        by_document.values(),
        key=lambda row: (row["invoice"].invoice_date or date.min, row["invoice"].pk),
        reverse=True,
    )
    for row in ordered:
        row["total_vat"] = row["total_ttc"] - row["total_ht"]
    return ordered


def _charge_panel(request, title, lines, invoices=()):
    """The documents behind a charge - a supplier's, or one of its postes -
    fetched when its row is opened, like a stock item's purchases. Each
    links to the document it came from.

    All of them, not the window the row totals: the row says what a charge
    costs now, this says what it has cost, which is what one opens it for.
    """
    rows = _charge_document_rows(lines, invoices)
    return render(
        request,
        "inventory/_charge_documents.html",
        {"title": title, "rows": rows, "total_ttc": sum((row["total_ttc"] for row in rows), Decimal("0"))},
    )


def _charge_curve(request, title, lines):
    """What a charge has cost over time, as the same chart a stock item's
    price history draws: a rent that moves, a subscription that doubles,
    seen at a glance. One point per document, against its own date."""
    # What was charged each day, added up: two bills of one date (two
    # meters, a statement and its regularisation) averaged into an amount
    # no document charged.
    by_date: dict = {}
    for row in _charge_document_rows(lines):
        day = row["invoice"].invoice_date
        if day is not None:
            by_date[day] = by_date.get(day, Decimal("0")) + row["total_ttc"]
    points = sorted(by_date.items())
    return render(
        request,
        "inventory/_charge_history.html",
        {
            "title": title,
            # Not a unit price: what each document of this charge came to,
            # tax included. The chart is the stock item's, the reading is
            # not, and the name it is given is all a screen reader gets.
            "chart_svg": _build_price_history_svg(points, f"Évolution de la charge : {title}"),
            "has_enough_data": len(points) >= 2,
        },
    )


def _poste_lines(product_id):
    from invoices.models import InvoiceLine

    product = get_object_or_404(Product, pk=product_id, is_expense=True)
    return product.raw_name, InvoiceLine.objects.filter(product=product).select_related("invoice", "invoice__supplier")


def _charge_supplier_lines(supplier_id):
    from invoices.models import InvoiceLine, Supplier

    supplier = get_object_or_404(Supplier, pk=supplier_id, expenses_only=True)
    return supplier, InvoiceLine.objects.filter(invoice__supplier=supplier).select_related(
        "invoice", "invoice__supplier"
    )


def charge_documents(request, product_id):
    return _charge_panel(request, *_poste_lines(product_id))


def charge_history(request, product_id):
    return _charge_curve(request, *_poste_lines(product_id))


def charge_supplier_documents(request, supplier_id):
    supplier, lines = _charge_supplier_lines(supplier_id)
    return _charge_panel(request, supplier.name, lines, supplier.invoices.select_related("supplier"))


def charge_supplier_history(request, supplier_id):
    supplier, lines = _charge_supplier_lines(supplier_id)
    return _charge_curve(request, supplier.name, lines)


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


def _search_normalize(text: str) -> str:
    """Case- AND accent-insensitive comparison key - "biere" has to find
    "Bière", since nobody reaches for the compose key while typing fast at a
    bar. Mirrors the normalisation static/js/datatable.js applies to every
    other table's search, so the two search boxes in this app behave the
    same way. SQLite's own `icontains` folds case but not accents, which is
    why this runs in Python instead."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def search_stock_types(request):
    """Backs the Stock page's search box: matches a stock type by its own
    name/category, or by the raw_name of any product filed under it, so
    typing what's actually printed on an invoice still finds the right row
    even when the stock type itself was named something more generic."""
    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse({"ids": []})
    needle = _search_normalize(query)
    ids = [
        stock_type.id
        for stock_type in StockType.objects.prefetch_related("products")
        if any(
            needle in _search_normalize(haystack)
            for haystack in [stock_type.name, stock_type.category]
            + [product.raw_name for product in stock_type.products.all()]
        )
    ]
    return JsonResponse({"ids": ids})


def export_associations(request):
    """Moved to « Données » (transfer/): the associations are one of the
    boxes there. The old address - a bookmark, a reverse() - opens that tab
    with them ticked."""
    return redirect(reverse("transfer:data_home") + "?cocher=associations")


def import_associations(request):
    """Moved to « Données »: its import tab takes the old
    marginmate-associations.json too (transfer/legacy.py)."""
    return redirect("transfer:data_import")


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
    target_id = request.POST.get("target_id", "")
    # A posted id that is not one is not found - not a server error.
    target = get_object_or_404(StockType, pk=target_id if is_id(target_id) else None)
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
    try:
        with transaction.atomic():
            stock_type.delete()
    except ProtectedError as exc:
        messages.error(
            request,
            f"« {name} » est encore utilisé ({_where_used(exc.protected_objects)}) : "
            "fusionnez-le dans un autre article plutôt que de le supprimer.",
        )
        return redirect("inventory:stock_list")
    # Product.stock_type is SET_NULL and StockMovement.stock_type is CASCADE,
    # so this alone sends every associated product back to the review queue
    # and drops the stock type's ledger entries. That cascade happens at the
    # DB level, bypassing unlink_product() - so invoice statuses need
    # refreshing separately here, or their invoices would stay marked
    # COMPLETE despite now containing an unreviewed product again.
    for product in affected_products:
        refresh_invoice_statuses_for_product(product)
    messages.success(request, f"Article « {name} » supprimé. Ses produits repassent dans « À classer ».")
    return redirect("inventory:stock_list")


def clear_empty_stock_types(request):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    # Empty: no product and no movement - a loss written down against an
    # item with no product would go with it.
    empty = StockType.objects.filter(products__isnull=True, movements__isnull=True).distinct()
    deleted = kept = 0
    for stock_type in empty:
        # One at a time: a syrup written into a recipe before its first
        # purchase is empty and in use, and must not stop the others.
        try:
            with transaction.atomic():
                stock_type.delete()
        except ProtectedError:
            kept += 1
        else:
            deleted += 1
    if deleted or kept:
        messages.success(
            request,
            f"{deleted} article(s) vide(s) supprimé(s)"
            + (f", {kept} gardé(s) car utilisé(s) dans une recette, un inventaire ou une vente." if kept else "."),
        )
    else:
        messages.info(request, "Aucun article vide à supprimer.")
    return redirect("inventory:stock_list")


def _where_used(objects) -> str:
    """What still names a stock item, in the words of the pages that do."""
    from recipes.models import RecipeIngredient, SaleDocumentLine

    recipes = sorted({obj.recipe.name for obj in objects if isinstance(obj, RecipeIngredient)})
    takes = sorted({timezone.localtime(obj.stock_take.taken_at).date() for obj in objects if isinstance(obj, StockTakeLine)})
    documents = {obj.document_id for obj in objects if isinstance(obj, SaleDocumentLine)}
    parts = []
    if recipes:
        parts.append("recette(s) " + ", ".join(recipes))
    if takes:
        parts.append("inventaire(s) du " + ", ".join(f"{day:%d/%m/%Y}" for day in takes))
    if documents:
        parts.append(f"{len(documents)} document(s) de vente")
    return " ; ".join(parts) or "ailleurs"


#: How many products the side panel lists at once; the next ones come up as
#: these are classified.
REVIEW_PANEL_SIZE = 50
#: The undo of a classification may delete the stock item it created - and
#: only that one: the id it posts back is signed with this.
UNDO_SALT = "inventory.undo-classification"


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def review_panel_context() -> dict:
    """The products no stock item has claimed, for the side panel."""
    # Every pending product gets a suggestion before the panel renders - no
    # separate button to click, no blank rows. Cheap on every visit: it only
    # touches products with no suggestion yet.
    apply_rules_to_pending_products()
    pending = (
        Product.objects.filter(stock_type__isnull=True, is_expense=False)
        .select_related("supplier")
        # Without the `__invoice` half, `{{ line.invoice.invoice_date }}`
        # hits the database once per invoice line (2439 of 2445 queries, ~5s,
        # before this was added).
        .prefetch_related("invoice_lines__invoice")
        .order_by("raw_name")
    )
    total = pending.count()
    stock_types = list(StockType.objects.order_by("name"))
    # The whole queue, not the products shown: "Tout approuver" and the
    # explanation both speak of all of it. Only the suggestions are fetched.
    all_suggestions = list(
        Product.objects.filter(stock_type__isnull=True, is_expense=False, ai_suggestion__isnull=False).values_list(
            "ai_suggestion", flat=True
        )
    )
    return {
        "review_products": list(pending[:REVIEW_PANEL_SIZE]),
        "review_total": total,
        "review_more": max(total - REVIEW_PANEL_SIZE, 0),
        "suggested_count": len(all_suggestions),
        "confidence_counts": Counter(s.get("confidence", "?") for s in all_suggestions),
        "fallback_count": sum(1 for s in all_suggestions if s.get("source") == "fallback"),
        "all_stock_types": stock_types,
        # What the panel's script needs to say whether a typed name is an
        # existing item, and of which unit and category.
        "stock_types_json": [
            {"name": st.name, "unit": st.unit, "unit_label": st.get_unit_display(), "category": st.category}
            for st in stock_types
        ],
        "unit_choices": UnitChoices.choices,
        "existing_categories": existing_categories(),
    }


def _review_panel(request, classified=None, status=200):
    """The side panel as the page swaps it in: the queue, what was just
    classified (with its undo), the messages of that action - said here, in
    the panel, rather than on the next page - and the navigation's count."""
    response = render(
        request,
        "inventory/_review_panel_refresh.html",
        {**review_panel_context(), "classified": classified, "show_messages": True},
        status=status,
    )
    return response


def review_queue(request):
    """The side panel alone for the page's script; anyone else is sent to
    the page, opened on it (the queue used to be a page of its own)."""
    if _is_htmx(request):
        return _review_panel(request)
    return redirect(reverse("inventory:stock_list") + "#a-classer")


def _catalogue_changed(response, stock_type_id):
    """Tell the page to reload its list, opened on this stock item."""
    response["HX-Trigger"] = json.dumps({"catalogue-changed": {"stock_type": stock_type_id}})
    return response


def remove_product(request, product_id):
    """Send a product back to the products to classify - from the list's
    "Retirer", or as the undo of a classification made in the panel, which
    also takes away the stock item that classification created (`drop_type`,
    signed) if nothing uses it."""
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    unlink_product(product)
    messages.success(request, f"« {product.raw_name} » repassé dans les produits à classer.")
    _drop_created_stock_type(request.POST.get("drop_type", ""))
    if _is_htmx(request):
        return _catalogue_changed(_review_panel(request), None)
    return redirect("inventory:stock_list")


def _drop_created_stock_type(signed: str) -> None:
    try:
        pk = signing.loads(signed, salt=UNDO_SALT)
    except signing.BadSignature:
        return
    stock_type = StockType.objects.filter(pk=pk, products__isnull=True, movements__isnull=True).first()
    if stock_type is None:
        return
    try:
        with transaction.atomic():
            stock_type.delete()
    except ProtectedError:
        # Written into a recipe or a count since: it stays.
        pass


def edit_product_conversion(request, product_id):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=None)
    if stock_equivalent is None:
        messages.error(request, "Facteur invalide.")
        return redirect("inventory:stock_list")
    if product.stock_type is None:
        messages.error(request, f"« {product.raw_name} » n'est rangé dans aucun article.")
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
        return redirect("inventory:stock_list")

    products = Product.objects.filter(stock_type__isnull=True, is_expense=False, ai_suggestion__isnull=False)
    approved = 0
    skip_reasons = Counter()
    for product in products:
        suggestion = product.ai_suggestion
        stock_equivalent = _parse_positive_decimal(str(suggestion.get("stock_equivalent", "")), default=None)
        stock_type = _resolve_suggestion_stock_type(suggestion)

        reason = None
        if stock_type is None:
            reason = "aucun article identifié"
        elif stock_equivalent is None:
            reason = f"facteur de conversion invalide ({suggestion.get('stock_equivalent')!r})"

        if reason:
            skip_reasons[reason] += 1
            # Clear it so the next time the panel is drawn a new suggestion is
            # made (review_panel_context calls apply_rules_to_pending_products,
            # which only ever touches products with no suggestion yet) instead
            # of it being permanently stuck with a bad one.
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
    return redirect("inventory:stock_list")


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
    """Classify one product under a stock item - an existing one named
    (case aside), or a new one with the unit and category given. From the
    panel, the panel comes back with the next product and a note saying
    where this one went, and the page reloads its list opened on it."""
    if request.method != "POST":
        return redirect("inventory:stock_list")

    product = get_object_or_404(Product, pk=product_id)
    name = request.POST.get("stock_type_name", "").strip()
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=Decimal("1"))

    error = None
    if product.is_expense:
        # A rent is not stock. The panel never offers one (it lists what
        # needs review, and a charge never does), but the address took it:
        # classified, the rent became bottles, with a stock movement behind.
        error = f"« {product.raw_name} » est un poste de charge : il ne se range dans aucun article."
    elif stock_equivalent is None:
        error = "« 1 produit = » doit être un nombre positif."
    elif not name:
        error = "Donnez le nom de l'article."
    if error:
        messages.error(request, error)
        return _review_panel(request) if _is_htmx(request) else redirect("inventory:stock_list")

    # A name matching an existing type (case-insensitively) is used as-is -
    # the unit/category fields only matter for creating a brand new one, the
    # same resolution _resolve_suggestion_stock_type already does for
    # suggestions.
    stock_type = StockType.objects.filter(name__iexact=name).first()
    created = stock_type is None
    if created:
        unit = request.POST.get("new_stock_type_unit") or UnitChoices.UNIT
        if unit not in UnitChoices.values:
            unit = UnitChoices.UNIT
        category = request.POST.get("new_stock_type_category", "").strip()
        stock_type = StockType.objects.create(name=name, unit=unit, category=category)

    # product.unit isn't a separate human choice: it always mirrors whatever
    # stock type ends up being used (existing types keep their own unit
    # regardless of what the "new type" dropdown said) - see
    # product_base_amount in services.py for why "Litre" vs "Kilogramme"
    # never actually changes anything, only "Unité" vs. not does.
    link_product_to_stock_type(product, stock_type, unit=stock_type.unit, stock_equivalent=stock_equivalent)
    if not _is_htmx(request):
        messages.success(request, f"« {product.raw_name} » rangé dans « {stock_type.name} ».")
        return redirect("inventory:stock_list")
    classified = {
        "product": product,
        "stock_type": stock_type,
        # 0.7, 10 - not 0.700, nor the 1E+1 normalize() makes of 10.
        "stock_equivalent": format(stock_equivalent.normalize(), "f"),
        "created": created,
        "undo": signing.dumps(stock_type.pk, salt=UNDO_SALT) if created else "",
    }
    return _catalogue_changed(_review_panel(request, classified), stock_type.pk)


def stock_take_variance(request, pk):
    """Where did the stock go? See inventory/variance.py for the reasoning -
    in short: what physically left the shelf, minus what the sales explain,
    with a recipe's alternatives pooled rather than guessed at.

    `?recettes=1` drops the items no recipe can reach. Those can only ever
    read as 100% missing - the till sells them but nothing says what they're
    made of - so they swamp the figure without being shrinkage at all. Both
    totals are computed either way, so the page can show what the filter
    costs rather than hiding it.
    """
    stock_take = get_object_or_404(StockTake, pk=pk)
    full = compute_variance(stock_take)
    linked = full.only_in_recipes()
    only_recipes = request.GET.get("recettes") == "1"
    return render(
        request,
        "inventory/stock_take_variance.html",
        {
            "report": linked if only_recipes else full,
            "only_recipes": only_recipes,
            "total_all": full.total_value_missing_min,
            "total_linked": linked.total_value_missing_min,
            "unlinked_count": full.unlinked_count,
        },
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


def _submitted_as_of(form) -> "date | None":
    """The date this save is counting, from the POST rather than from the
    saved row: the date field and a new line can change in the same submit,
    and the new line has to be judged against the date being saved."""
    if not form.is_bound or not form.is_valid():
        return None
    taken_at = form.cleaned_data.get("taken_at")
    return timezone.localtime(taken_at).date() if taken_at else None


def _stock_take_form_view(request, stock_take):
    if request.method == "POST":
        form = StockTakeForm(request.POST, instance=stock_take)
        # One resolver for the whole formset: every row asks the same "what
        # did the user type, and did it exist yet" questions of the same data,
        # and asking per row was a query per row (400 on a real inventory).
        form_kwargs = {"as_of": _submitted_as_of(form), "resolver": EntryResolver()}
        formset = StockTakeLineFormSet(request.POST, instance=stock_take, form_kwargs=form_kwargs)
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
            # A draft kept in the browser may still name an article the old
            # way; the page renames it as it restores it.
            "entry_suffix": STOCK_TYPE_ENTRY_SUFFIX,
            "old_entry_suffixes": list(OLD_STOCK_TYPE_ENTRY_SUFFIXES),
            # What the already-saved lines are worth, so the running total is
            # right the moment the page opens without valuing anything again
            # (a saved line's value is frozen - see StockTake's docstring).
            "saved_values": json.dumps(
                {
                    str(line_form.instance.pk): str(line_form.instance.value_ht)
                    for line_form in formset.forms
                    if line_form.instance.pk and line_form.instance.value_ht is not None
                }
            ),
        },
    )


def value_stock_take_line(request):
    """Price one row of the inventory being edited, live.

    Deliberately routed through the very same functions the save uses
    (value_counted_quantity / value_counted_stock_type_quantity, as of the
    same date), because a preview computed a second, simpler way is exactly
    how this codebase has shipped silently wrong money before. What the row
    shows while typing is what the row will be worth once saved.

    A GET: it reads and prices, it changes nothing.
    """
    name = (request.GET.get("entry") or "").strip()
    resolver = EntryResolver()
    try:
        quantity = Decimal((request.GET.get("quantity") or "").replace(",", "."))
    except InvalidOperation:
        return JsonResponse({"ok": False, "error": "quantity"})
    as_of = parse_date(request.GET.get("as_of") or "") or timezone.localdate()

    if is_stock_type_entry(name):
        stock_type = resolver.stock_type(name)
        if stock_type is None:
            return JsonResponse({"ok": False, "error": "unknown"})
        result = value_counted_stock_type_quantity(stock_type, quantity, as_of=as_of)
        unit_label = stock_type.get_unit_display()
    else:
        product = resolver.product(name)
        if product is None:
            return JsonResponse({"ok": False, "error": "unknown"})
        unit = request.GET.get("unit") or UnitChoices.UNIT
        if unit not in {UnitChoices.UNIT, product.stock_type.unit}:
            unit = UnitChoices.UNIT
        result = value_counted_quantity(product, quantity, unit, as_of=as_of)
        unit_label = "unité" if unit == UnitChoices.UNIT else product.stock_type.get_unit_display()

    value_ht = result["value_ht"]
    return JsonResponse(
        {
            "ok": True,
            "value_ht": f"{value_ht:.2f}",
            # The price of ONE of whatever was counted, which is the number a
            # human recognises ("a bottle of that is 16.86 €") - and it is
            # the count's own blended FIFO price, not a headline list price,
            # so it always reconciles with the line total beside it.
            "unit_cost_ht": f"{(value_ht / quantity):.2f}" if quantity else None,
            "unit_label": unit_label,
            "has_shortfall": result["has_shortfall"],
            "shortfall_quantity": f"{result['shortfall_quantity']:.2f}",
        }
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
