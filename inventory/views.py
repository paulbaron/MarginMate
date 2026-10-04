import json
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.contrib.messages import get_messages
from django.core import signing
from django.db import transaction
from django.db.models import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import escape
from django.utils.http import urlencode
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from accounts.access import access_of
from common import (
    AMBIGUOUS_THOUSANDS,
    DateRange,
    date_range,
    format_money,
    group_thousands,
    is_id,
    read_amount,
    read_number,
    search_key,
)

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
from .gaps import (
    MAX_AMOUNT,
    duration_words,
    entry_lines,
    entry_rows,
    fill_gaps,
    gaps_since,
    list_counts,
    list_is_stale,
    sales_watched_from,
    servings_from,
    share_summary,
    show_list,
)
from .models import (
    GapExclusion,
    GapFillEntry,
    GapFillSetting,
    MovementKind,
    Product,
    StockMovement,
    StockTake,
    StockTakeLine,
    StockTakeLineSource,
    StockType,
    UnitChoices,
)
from .product_matching_rules import SuggestionContext, apply_rules_to_pending_products, is_current, suggest_for_product
from .services import (
    conversion_refusal,
    link_product_to_stock_type,
    merge_stock_types,
    product_base_amount,
    refresh_invoice_statuses_for_product,
    unlink_product,
    update_product_conversion,
    value_counted_quantity,
    value_counted_stock_type_quantity,
)
from .variance import (
    PeriodStock,
    SoldQuantity,
    StockPeriod,
    compute_variance,
    quantities_sold,
    stock_between,
)


def existing_categories():
    return StockType.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")


# Big enough to appear nowhere else in a URL.
_PK_MARKER = 2147483647


def pk_url(name: str):
    """`reverse(name, args=[pk])` as a function of `pk`, for one reverse().

    A list of 400 stock items asking `{% url %}` four times a row spent a
    third of its rendering resolving URLs. The URL of an `<int:pk>` route is
    the same string around the pk's digits, so it is reversed once with a
    marker and the digits are put in its place. Only for a route whose one
    argument is an int pk; anything else would not be the same string.
    """
    url = reverse(name, args=[_PK_MARKER])
    marker = str(_PK_MARKER)
    if url.count(marker) != 1:
        return lambda pk: reverse(name, args=[pk])
    head, tail = url.split(marker)
    return lambda pk: f"{head}{int(pk)}{tail}"


class StockListView(TemplateView):
    """ "Produits & charges": every stock item and what was spent on it, the
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


def _window_label(window: DateRange) -> str:
    """« du 01/02/2026 au 28/02/2026 » - what the page says it is showing.

    Either end alone is a window a person asks for, and « du 01/02/2026 au »
    reads as a page that lost half of its own question. The three phrasings
    are built once here rather than as three `{% if %}` in each of the five
    places on this page that have to name the window.
    """
    start = window.start.strftime("%d/%m/%Y") if window.start else ""
    end = window.end.strftime("%d/%m/%Y") if window.end else ""
    if start and end:
        return f"du {start} au {end}"
    if start:
        return f"depuis le {start}"
    if end:
        return f"jusqu'au {end}"
    return ""


def _panel_doors(request, bare_url: str) -> dict:
    """What a row's panel applies, and the two doors out of it.

    « tout l'historique » dropped the dates from the URL and nothing on
    screen offered them back - and a row is fetched exactly once
    (stock_list.html marks it loaded), so closing and reopening it did not
    bring them back either: the panel went on answering « tout » under
    « Acheté 12 » until the whole page was reloaded, which is the very
    contradiction this window exists to remove. So the link carries the
    dates with it under `tout=1` - « everything, and remember these dates » -
    and the whole history offers them back.

    Shared by the two panels so neither can grow its own idea of the door.
    """
    asked = date_range(request)
    showing_all = bool(request.GET.get("tout"))
    applied = DateRange() if showing_all else asked
    query = urlencode(asked.parameters)
    return {
        "window": applied,
        "window_label": _window_label(applied),
        "all_url": f"{bare_url}?{query}&tout=1" if asked else bare_url,
        # No dates asked, no way back to offer: a panel nobody narrowed says
        # nothing about windows at all, as it did before any of this.
        "back_url": f"{bare_url}?{query}" if showing_all and asked else None,
        "back_label": _window_label(asked),
    }


def catalogue_context(request) -> dict:
    """The list: categories of stock items with what was bought and sold over
    all time, over the window `?du=&au=` names, or what moved over the period
    `?inventaire=` names."""
    period = selected_period(request)
    asked = date_range(request)
    # A stock take is not two dates: it is two physical counts, and the
    # figures it produces - opening, closing, what left the shelf, what is
    # missing - come out of them. A free pair of dates cannot produce those,
    # so the stock take wins and the page says so, rather than drawing one
    # period's arithmetic under another period's dates.
    window = DateRange() if period is not None else asked
    stock_types = list(StockType.objects.all().order_by("category", "name"))
    context = {"date_window": asked, "date_window_label": _window_label(asked)}

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
    #
    # The window « du … au … » is applied inside this same scan, on columns
    # fetched with it: StockMovement.effective_date is a PROPERTY, so
    # narrowing by it over model instances would be a query per movement.
    # Its fallback order is reproduced exactly below - the movement's own
    # date, else the invoice's (when the stock arrived, not when the PDF
    # happened to be imported), else when it was typed in - and a movement
    # no date can be found for is in no window at all. With no window (all
    # time, or a stock take) the three dates decide nothing and are not
    # fetched: parsing them for every movement, and the join to the invoice,
    # were 40 % of this scan.
    quantity_by_type: dict[int, Decimal] = {}
    value_ht_by_type: dict[int, Decimal] = {}
    ledger_in_window: dict[int, Decimal] = {}
    bought_by_type: dict[int, Decimal] = {}
    bought_ht_by_type: dict[int, Decimal] = {}
    value_ttc_by_type: dict[int, Decimal] = {}
    columns = [
        "stock_type_id",
        "kind",
        "quantity",
        "unit_cost_ht",
        "invoice_line__total_ht",
        "invoice_line__vat_rate",
        "invoice_line__printed_ttc",
        "invoice_line__discount_ttc",
    ]
    if window:
        columns += ["occurred_on", "invoice_line__invoice__invoice_date", "created_at"]
    for (
        stock_type_id,
        kind,
        quantity,
        unit_cost_ht,
        line_total_ht,
        vat_rate,
        printed_ttc,
        discount_ttc,
        *dates,
    ) in StockMovement.objects.values_list(*columns):
        quantity_by_type[stock_type_id] = quantity_by_type.get(stock_type_id, Decimal("0")) + quantity
        value_ht_by_type[stock_type_id] = value_ht_by_type.get(stock_type_id, Decimal("0")) + (quantity * unit_cost_ht)
        # The two sums above are all time whatever the window: they are what
        # an article COSTS per unit, and a price is not a property of a
        # window - valued over a window holding one delivery, every bottle
        # would be worth what that delivery charged.
        if window:
            occurred_on, invoice_date, created_at = dates
            if not window.holds(occurred_on or invoice_date or (created_at.date() if created_at else None)):
                continue
        ledger_in_window[stock_type_id] = ledger_in_window.get(stock_type_id, Decimal("0")) + quantity
        if kind != MovementKind.PURCHASE:
            continue
        bought_by_type[stock_type_id] = bought_by_type.get(stock_type_id, Decimal("0")) + quantity
        # « Total HT » is the line's own amount, the one the document prints
        # and the one the panel under the row lists - not quantity times
        # unit_cost_ht, which is that amount divided by the quantity and
        # stored to four decimals, then multiplied back: 2 000 units at a
        # real 0,01442 are priced 0,0144 and come back 4 cents short of
        # the 28,84 € the invoice charges, and the row read 28,80 € over a
        # panel whose lines add up to 28,84 €. Only where there is no line at all - a
        # correction typed by hand - is there nothing but the ledger's own
        # arithmetic to go on. (`value_ht_by_type` above stays that
        # arithmetic whatever happens: it is divided by the quantity to
        # price a unit, the way StockType.current_unit_cost_ht does, and
        # must keep saying what the ledger holds.)
        bought_ht_by_type[stock_type_id] = bought_ht_by_type.get(stock_type_id, Decimal("0")) + (
            line_total_ht if line_total_ht is not None else quantity * unit_cost_ht
        )
        if line_total_ht is not None:
            # InvoiceLine.total_ttc reproduced from the columns this scan
            # already fetches - the amount the document PRINTED where there
            # is one, less the promotion beside it, and HT times the rate
            # only where nothing was printed. Worked out from HT alone, a
            # receipt printing 39,99 € was counted 40,00 € on the row
            # while the panel it opens printed 39,99 € under it: one
            # purchase, two answers, and the panel is now the row's own
            # lines rather than a different history.
            value_ttc_by_type[stock_type_id] = value_ttc_by_type.get(stock_type_id, Decimal("0")) + (
                printed_ttc - discount_ttc if printed_ttc is not None else line_total_ht * (vat_rate + Decimal("1"))
            )

    movements_url = pk_url("inventory:stock_type_movements")
    update_url = pk_url("inventory:stock_type_update")
    price_history_url = pk_url("inventory:stock_type_price_history")
    delete_url = pk_url("inventory:stock_type_delete")
    rows = [
        {
            "stock_type": st,
            "quantity": bought_by_type.get(st.id, Decimal("0")),
            "value_ht": bought_ht_by_type.get(st.id, Decimal("0")),
            "value_ttc": value_ttc_by_type.get(st.id, Decimal("0")),
            # What _catalogue.html prints on every row, worked out once here:
            # four {% url %} and seven localised ids a row were most of the
            # time the list took to render. `id` is the digits a template
            # prints an int as.
            "id": str(st.id),
            "unit_label": st.get_unit_display(),
            "movements_url": movements_url(st.pk),
            "update_url": update_url(st.pk),
            "price_history_url": price_history_url(st.pk),
            "delete_url": delete_url(st.pk),
        }
        for st in stock_types
        # « Only see the products bought between two dates »: an
        # article nothing was bought of over the window is not a row of
        # zeroes to scroll past, it is not part of the answer at all. Its
        # category goes with it, having nothing left in it.
        if not window or st.id in bought_by_type
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
    # The articles the window holds, not every article there is: a count
    # over one window beside a list over another says 316 above a page of 3.
    context["stock_type_count"] = len(rows)
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
    context["period"] = period
    context["stock_takes"] = StockTake.objects.all()
    if period is not None:
        sold = quantities_sold(period.start, period.end, unit_costs=unit_costs, available=period.ceilings())
    else:
        # The ledger quantities are the ceiling: what was bought (the
        # "Acheté" column beside "Vendu"), less the losses recorded, which
        # cannot have been sold - over the SAME window as the sales, or the
        # column lies. All-time purchases against one month of sales would
        # say nothing is ever missing (see quantities_sold).
        if window:
            # Floored at zero the way PeriodStock.sellable is, and for the
            # same reason: a loss written down inside the window against
            # stock bought before it would give a negative ceiling, and a
            # negative ceiling lets an item soak up sales it never covered.
            available = {
                stock_type_id: max(Decimal("0"), quantity) for stock_type_id, quantity in ledger_in_window.items()
            }
        else:
            available = quantity_by_type
        # sales_between EXCLUDES its start day - a sale on the day of a
        # stock count belongs to the period that count closes. Here the
        # start is a date a person typed, « du 1er » means the 1st, and
        # handing it straight over would silently drop that day's sales. So
        # the day before goes in its place.
        sold = quantities_sold(
            # The calendar's first day has no day before it: from the
            # beginning is the same sales.
            window.start - timedelta(days=1) if window.start and window.start > date.min else None,
            window.end,
            unit_costs=unit_costs,
            available=available,
        )
    _attach_sold(context, categories, sold, period, unit_costs)

    context["review_count"] = Product.objects.filter(stock_type__isnull=True, is_expense=False).count()
    context["empty_stock_type_count"] = StockType.objects.filter(products__isnull=True).distinct().count()
    # The list reloads itself in place after a product is classified
    # (_catalogue.html's hx-get), and stock_catalogue reads the request
    # again - so the URL is the whole of what keeps the reader where they
    # were. Built here: two optional parameters concatenated by `{% if %}`
    # in the template was already awkward with one.
    reload_parameters = {}
    if period is not None:
        reload_parameters["inventaire"] = period.closing_take.pk
    reload_parameters.update(asked.parameters)
    context["catalogue_url"] = reverse("inventory:stock_catalogue") + (
        f"?{urlencode(reload_parameters)}" if reload_parameters else ""
    )
    # What a row OPENS is the window too, or the panel contradicts the row
    # it explains: « Acheté 12 » above forty purchases. One string appended
    # to the three data-movements-url of _catalogue.html rather than an
    # `{% if %}` fragment per attribute - which is where one of the three
    # ends up being the one that forgets it.
    #
    # From `window` and NOT from `asked`: under a chosen stock take the
    # dates are disabled and the panels stay all-history (CLAUDE.md, « what
    # a row opens is the whole history »), and building this from the
    # window APPLIED is what makes that true without a second condition.
    context["panel_window_query"] = f"?{urlencode(window.parameters)}" if window else ""
    context["charge_suppliers"] = charge_suppliers(period if period is not None else window)
    context["charge_total_ttc"] = sum((row["total_ttc"] for row in context["charge_suppliers"]), Decimal("0"))
    # What the charges' « Documents » column counts over, said once for its
    # header and for each row's count: on a phone the header row is not
    # drawn and every row says it itself (data-label, _catalogue.html) -
    # « 12 » read as every bill there is was the 20/09 confusion (« Free est
    # dit avoir 12 documents alors qu'en réalité il y en a plus »).
    # « période » is this page's word for two counts, never for two dates.
    if period is not None:
        context["charge_documents_heading"] = "Documents (période)"
    elif asked:
        context["charge_documents_heading"] = "Documents (ces dates)"
    else:
        context["charge_documents_heading"] = "Documents (12 mois)"
    return context


def charge_suppliers(window=None) -> list[dict]:
    """What the suppliers of charges cost - a subscription, the rent, the
    water. They hold no stock, so they are nowhere else on this page; they
    are spending all the same, and seeing it beside the purchases is the
    whole point of showing them here.

    Over the window being looked at, or the last twelve months by default:
    an all-time total of a monthly subscription says little.

    One window, two shapes, because both say the same two things: a
    StockPeriod (« l'inventaire du 31/03 », whose `start` is None for the
    very first one) and a DateRange (« du 01/02 au 28/02 », whose `end` may
    be None for « depuis le 1er février »). An empty DateRange is falsy and
    means nobody asked for a window - which is the twelve months. A second
    parameter for the second shape would only have let the two disagree
    about which end is included: here both are, as they were.

    **Every supplier is a row that opens**, on its documents and on its
    curve, exactly as a stock item does - the water bill has one charge item
    and the rent statement six, and a page that only opened the second left
    five suppliers out of six with nothing to click. Where a document names
    several charge items, those are rows of their own underneath (the rent
    apart from the building provision, which is the one that gets
    regularised - see invoices.charges); where it names one, the supplier's
    row *is* that charge item and repeating it below would say the same
    thing twice.

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
    if window:
        # The first stock take's window has no start: it runs from the
        # beginning, and a None in the filter was a 500. « Jusqu'au 28 » has
        # no start either, and « depuis le 1er » no end.
        if window.end is not None:
            documents = documents.filter(invoice_date__lte=window.end)
        if window.start is not None:
            documents = documents.filter(invoice_date__gte=window.start)
        since = window.start
    else:
        since = timezone.localdate() - timedelta(days=365)
        documents = documents.filter(invoice_date__gte=since)
    rows: dict[int, dict] = {
        supplier.pk: {
            "supplier": supplier,
            "documents": 0,
            "documents_all": 0,
            "total_ttc": Decimal("0"),
            "last": None,
            "charge_items": {},
        }
        for supplier in suppliers
    }
    # Only what is read below: a line's invoice is its supplier here, and
    # selected whole it brought the document's whole text along with it.
    lines = (
        InvoiceLine.objects.filter(invoice__in=documents)
        .select_related("invoice", "product")
        .only("raw_name", "total_ht", "vat_rate", "printed_ttc", "discount_ttc", "invoice__supplier", "product")
    )
    for line in lines:
        row = rows[line.invoice.supplier_id]
        # The line's own amount, which for a charge is the figure the
        # document prints (InvoiceLine.printed_ttc): 33,33 € HT at 20% works
        # back out to 40,00 € where the bill says 39,99 €.
        amount = line.total_ttc.quantize(Decimal("0.01"))
        row["total_ttc"] += amount
        charge_item = row["charge_items"].setdefault(
            line.raw_name,
            {"name": line.raw_name, "product": line.product, "total_ttc": Decimal("0"), "invoices": set()},
        )
        charge_item["total_ttc"] += amount
        charge_item["invoices"].add(line.invoice_id)
    for supplier_id in documents.values_list("supplier_id", flat=True):
        rows[supplier_id]["documents"] += 1
    # The last document ever, inside the window or not: it is what says a
    # supplier has gone quiet, and on a row showing nothing over the window
    # it is the only thing left to say. How many there are in all is said
    # beside the window's count: « Free est dit avoir 12 documents alors
    # qu'en réalité il y en a plus » - twelve is a year of a monthly
    # subscription, and the row opens on all 33 of them (owner, 20/09).
    for supplier_id, invoice_date in every_document.values_list("supplier_id", "invoice_date"):
        row = rows[supplier_id]
        row["documents_all"] += 1
        if invoice_date is not None and (row["last"] is None or invoice_date > row["last"]):
            row["last"] = invoice_date
    return [
        row
        | {
            "since": since,
            # One charge item is the charge itself under another name, and the
            # supplier's own row already opens on it.
            "charge_items": sorted(
                (
                    charge_item | {"documents": len(charge_item["invoices"])}
                    for charge_item in row["charge_items"].values()
                    # A line nobody attached to a product cannot be opened;
                    # it is still counted in the supplier's total above.
                    if charge_item["product"] is not None
                ),
                key=lambda charge_item: -charge_item["total_ttc"],
            )
            if len(row["charge_items"]) > 1
            else [],
        }
        for row in rows.values()
        # Filed anything at all - counted just above, whatever the window.
        if row["documents_all"]
    ]


def selected_period(request) -> StockPeriod | None:
    """The window `?inventaire=<pk>` asks for, or None for all time.

    Only the CLOSING count is named; the opening one is whichever came
    before it, exactly as on the variance page - naming both would let the
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


def _stock_type_movement_entries(stock_type, window: DateRange | None = None):
    """The purchases behind one article's row, narrowed to `window`.

    Narrowed on `effective_date` - the movement's own date, else its
    invoice's, else when it was typed in - because that is exactly what
    catalogue_context sums the row's « Acheté » and its totals by. Filtered
    on the invoice's date instead, the panel would not add up to the row it
    was opened to explain, and a panel disagreeing with its own row is the
    silently-wrong-money shape this codebase keeps getting bitten by.

    `effective_date` is a property, so the catalogue's scan over every
    movement there is reads its columns by hand; here it is one article's
    ledger with its invoice already selected, and reading the property per
    movement costs no query.
    """
    window = window or DateRange()
    movements_qs = (
        StockMovement.objects.filter(stock_type=stock_type)
        .select_related("invoice_line__invoice__supplier", "invoice_line__product")
        .order_by("-invoice_line__invoice__invoice_date", "-created_at")
    )
    # Three links a line, reversed once each rather than once a line.
    invoice_url = pk_url("invoices:invoice_detail")
    conversion_url = pk_url("inventory:edit_product_conversion")
    remove_url = pk_url("inventory:remove_product")
    entries = []
    for m in movements_qs:
        if not window.holds(m.effective_date):
            continue
        line = m.invoice_line
        entries.append(
            {
                "movement": m,
                "line": line,
                "invoice_url": invoice_url(line.invoice_id) if line else None,
                "conversion_url": conversion_url(line.product.id) if line else None,
                "remove_url": remove_url(line.product.id) if line else None,
                # The date this movement was SELECTED by, which is the one
                # to print: the column showed the invoice's date and "—"
                # for a manual correction, and a row picked out by two dates
                # then read as a row with no date at all.
                "date": m.effective_date,
                # Said only where it is not a purchase: the row above counts
                # « Acheté » on purchases alone, so a broken bottle listed
                # here with nothing marking it is a line the reader adds to
                # a figure that never held it. Marking every line « Achat »
                # would hide the exception again.
                "kind_label": None if m.kind == MovementKind.PURCHASE else m.get_kind_display(),
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
    # Ordered by the date the column PRINTS. The query orders by the
    # invoice's date, which the panel stopped showing: a delivery invoiced
    # on 20/01 and received on the 27th came out between the 3rd and the
    # 14th, and a dated column out of order reads as a bug. Python's sort is
    # stable, so two movements of one day keep the query's own order.
    entries.sort(key=lambda entry: entry["date"] or date.min, reverse=True)
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
        f'data-x="{x:.1f}" data-y="{y:.1f}" data-label="{d:%d/%m/%Y}" data-value="{format_money(p, ".4f")} €" />'
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
        f'<text x="4" y="{pad_top + 4}" font-size="11" fill="var(--muted)">{format_money(max_price)} €</text>'
        f'<text x="4" y="{height - pad_bottom}" font-size="11" fill="var(--muted)">{format_money(min_price)} €</text>'
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
    raw_movements = StockMovement.objects.filter(stock_type=stock_type, invoice_line__isnull=False).values_list(
        "invoice_line__invoice__invoice_date", "quantity", "unit_cost_ht"
    )
    points = _aggregate_price_points(raw_movements)
    return render(
        request,
        "inventory/_stock_type_price_history.html",
        {"stock_type": stock_type, "chart_svg": _build_price_history_svg(points), "has_enough_data": len(points) >= 2},
    )


def _charge_document_rows(lines, invoices=()) -> list[dict]:
    """One row per document, from the lines given - one charge item's, or a
    whole supplier's.

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
        # Rounded to the cent line by line, exactly as charge_suppliers
        # rounds the row this panel explains. `total_ttc` works 19,99 € HT
        # at 20 % out to 23,988 €: added raw, twelve of them foot 287,86 €
        # under twelve visible lines of « 23,99 € » and beside a row saying
        # 287,88 €. Two roundings of one figure is two answers. (Invented
        # figures: a real subscription's price does not belong in a public
        # repository, even as an example.)
        row["total_ttc"] += line.total_ttc.quantize(Decimal("0.01"))
    ordered = sorted(
        by_document.values(),
        key=lambda row: (row["invoice"].invoice_date or date.min, row["invoice"].pk),
        reverse=True,
    )
    for row in ordered:
        row["total_vat"] = row["total_ttc"] - row["total_ht"]
    return ordered


def _charge_panel(request, title, all_url, lines, invoices=None):
    """The documents behind a charge - a supplier's, or one of its charge
    items - fetched when its row is opened, like a stock item's purchases.
    Each links to the document it came from.

    Narrowed to the « Du … au … » the row was counted over, on
    `invoice_date` - the very filter charge_suppliers uses - so the panel
    adds up to the row above it. The documents filed with nothing read go
    through the same filter: they count on the row, so they follow the row.

    Asked with no window it is the whole history, as it always was: the
    twelve-month default the fold falls back on is not a window a person
    typed, and a subscription opened to be followed month after month has
    to show the months.
    """
    doors = _panel_doors(request, all_url)
    window = doors["window"]
    lines = window.limit(lines, "invoice__invoice_date")
    invoices = window.limit(invoices, "invoice_date") if invoices is not None else ()
    rows = _charge_document_rows(lines, invoices)
    return render(
        request,
        "inventory/_charge_documents.html",
        {
            "title": title,
            "rows": rows,
            "total_ttc": sum((row["total_ttc"] for row in rows), Decimal("0")),
            **doors,
        },
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


def _charge_item_lines(product_id):
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
    title, lines = _charge_item_lines(product_id)
    return _charge_panel(request, title, reverse("inventory:charge_documents", args=[product_id]), lines)


def charge_history(request, product_id):
    return _charge_curve(request, *_charge_item_lines(product_id))


def charge_supplier_documents(request, supplier_id):
    supplier, lines = _charge_supplier_lines(supplier_id)
    return _charge_panel(
        request,
        supplier.name,
        reverse("inventory:charge_supplier_documents", args=[supplier_id]),
        lines,
        supplier.invoices.select_related("supplier"),
    )


def charge_supplier_history(request, supplier_id):
    supplier, lines = _charge_supplier_lines(supplier_id)
    return _charge_curve(request, supplier.name, lines)


def stock_type_movements(request, pk):
    """Purchase history for one stock type - fetched on demand (see
    StockListView.get_context_data for why this isn't just baked into the
    main page for every stock type up front) the first time its row is
    expanded, via a plain hx-get/htmx.ajax call from stock_list.html.

    The window comes back in the URL the row carries (`panel_window_query`),
    and is read here the way stock_catalogue reads it: the request is all
    this view is given, and a panel that ignored it would answer « Acheté
    12 » with forty purchases.
    """
    stock_type = get_object_or_404(StockType, pk=pk)
    doors = _panel_doors(request, reverse("inventory:stock_type_movements", args=[stock_type.pk]))
    entries = _stock_type_movement_entries(stock_type, doors["window"])
    return render(
        request,
        "inventory/_stock_type_movements.html",
        {
            "stock_type": stock_type,
            # Printed on every line, twice.
            "unit_label": stock_type.get_unit_display(),
            "movements": entries,
            # The header is a claim in French about what is under it, and
            # the row above counts purchases alone: « Achats » over a broken
            # bottle says this page bought one.
            "other_movements": any(entry["kind_label"] for entry in entries),
            **doors,
        },
    )


def _search_normalize(text: str) -> str:
    """Case- AND accent-insensitive comparison key - "biere" has to find
    "Bière", since nobody reaches for the compose key while typing fast at a
    bar. Mirrors the normalisation static/js/datatable.js applies to every
    other table's search, so the search boxes in this app behave the same
    way. SQLite's own `icontains` folds case but not accents, which is why
    this runs in Python instead.

    `common.search_key` is the one definition, shared since the documents'
    search needed the same answer: two normalisers that could drift are two
    boxes that stop agreeing about what « biere » finds."""
    return search_key(text)


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
        products = list(self.object.products.all()) if self.object.unit != old_unit else []
        # Asked before anything is saved: from Unité to Litre a line's measured
        # volume becomes what its cost is divided by, and a tiny one makes a
        # unit cost no stock movement holds.
        refused = f"Unité « {self.object.get_unit_display()} » refusée"
        for product in products:
            refusal = conversion_refusal(product, self.object.unit, product.stock_equivalent, refused=refused)
            if refusal:
                form.add_error("unit", refusal)
                return self.form_invalid(form)
        response = super().form_valid(form)
        if self.object.unit != old_unit:
            for product in products:
                update_product_conversion(product, unit=self.object.unit, stock_equivalent=product.stock_equivalent)
            if products:
                messages.info(
                    self.request,
                    f'Unité changée : {len(products)} produit(s) de "{self.object.name}" recalculé(s).',
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
    if target.pk == source.pk:
        # Never offered by the page; posted by hand, it deleted the item with
        # its losses and counts - what delete_stock_type refuses.
        messages.error(request, "Fusion impossible : choisissez un autre article que celui-ci.")
        return redirect("inventory:stock_type_update", pk=source.pk)
    if source.unit != target.unit:
        messages.error(
            request,
            f'Fusion impossible : "{source.name}" est en {source.get_unit_display()}, '
            f"\"{target.name}\" est en {target.get_unit_display()}. Changez d'abord l'unité "
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
    takes = sorted(
        {timezone.localtime(obj.stock_take.taken_at).date() for obj in objects if isinstance(obj, StockTakeLine)}
    )
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
    # touches products whose stored suggestion is missing or was made against
    # classifications that have moved since (`is_current`) - a neighbour
    # re-filed, unlinked, an article gone - so a stale « haute » is never
    # drawn, let alone approved.
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
    confidence_counts = Counter(s.get("confidence", "?") for s in all_suggestions)
    return {
        "review_products": list(pending[:REVIEW_PANEL_SIZE]),
        "review_total": total,
        "review_more": max(total - REVIEW_PANEL_SIZE, 0),
        "suggested_count": len(all_suggestions),
        "confidence_counts": confidence_counts,
        # What « Approuver les N sûres » will take: the same test
        # approve_all_suggestions applies (SURE_CONFIDENCE), counted here so
        # the button's label and its confirmation say the same number.
        "sure_count": confidence_counts.get(SURE_CONFIDENCE, 0),
        "source_counts": Counter(s.get("source", "?") for s in all_suggestions),
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
        messages.error(request, "Facteur invalide : un nombre positif d'au plus 4 décimales est attendu.")
        return redirect("inventory:stock_list")
    if product.stock_type is None:
        messages.error(request, f"« {product.raw_name} » n'est rangé dans aucun article.")
        return redirect("inventory:stock_list")
    refusal = conversion_refusal(product, product.stock_type.unit, stock_equivalent)
    if refusal:
        messages.error(request, refusal)
        return redirect("inventory:stock_list")
    # product.unit always mirrors its stock type's unit now (see
    # assign_product) - there's nothing left for a human to choose here
    # beyond the conversion factor itself.
    update_product_conversion(product, unit=product.stock_type.unit, stock_equivalent=stock_equivalent)
    # 0.7, 24 - not the 0.7000, 24.0000 read_amount quantizes to.
    factor = format(stock_equivalent.normalize(), "f")
    messages.success(request, f'"{product.raw_name}" mis à jour (facteur {factor}, {product.stock_type}).')
    return redirect("inventory:stock_list")


def _resolve_suggestion_stock_type(suggestion: dict) -> StockType | None:
    """The article a suggestion names: the existing one it matched, or the
    new one it describes - unsaved, made by the caller once the suggestion
    is taken (a refused one left an empty article on the Stock page). One
    that matched an article since deleted (an undo, a merge) names NOTHING -
    made again by name it would resurrect what somebody removed, silently,
    under « Approuver »."""
    matched_id = suggestion.get("matched_stock_type_id")
    if matched_id:
        stock_type = StockType.objects.filter(pk=matched_id).first()
        if stock_type:
            return stock_type
        if not suggestion.get("is_new_stock_type", True):
            return None
    name = (suggestion.get("stock_type_name") or "").strip()
    if not name:
        return None
    unit = suggestion.get("new_stock_type_unit") or UnitChoices.UNIT
    category = (suggestion.get("new_stock_type_category") or "").strip()
    return StockType.objects.filter(name__iexact=name).first() or StockType(name=name, unit=unit, category=category)


# The one confidence « Approuver les N sûres » takes: what the leave-one-out
# benchmark (product_matching_rules, scratchpad loo_pipeline.py) showed right
# on article, category and conversion factor for every product it named.
SURE_CONFIDENCE = "high"
# What the panel's form may post as `confiance`: everything, or the sure ones.
APPROVE_SCOPES = {"": None, "haute": SURE_CONFIDENCE}


def approve_all_suggestions(request):
    """Link every pending product to its suggestion - all of them, or with
    `confiance=haute` only those the pipeline is sure of. Read at POST time,
    so a product classified meanwhile (another tab, the panel itself) is
    simply no longer pending and is left as it was classified.

    A stored suggestion is a claim about the classifications at the moment
    the panel was drawn. One made against classifications that have moved
    since (`is_current`: a neighbour re-filed, an undo, a merge, a rename) is
    made again here, against the classifications as they stand at the
    click - this is the moment money is booked - and only booked if it still
    meets the scope; otherwise it is left to classify, with its new
    suggestion stored. The classifications this very pass makes do not feed
    it: the batch is approved as the panel showed it."""
    if request.method != "POST":
        return redirect("inventory:stock_list")
    scope = request.POST.get("confiance", "")
    if scope not in APPROVE_SCOPES:
        messages.error(request, "Choix inconnu : rien n'a été approuvé.")
        return redirect("inventory:stock_list")
    only_sure = APPROVE_SCOPES[scope] is not None

    products = Product.objects.filter(stock_type__isnull=True, is_expense=False, ai_suggestion__isnull=False)
    if only_sure:
        products = products.filter(ai_suggestion__confidence=APPROVE_SCOPES[scope])
    context = SuggestionContext()
    approved = 0
    remade = 0
    no_longer_sure = 0
    no_factor = 0
    skip_reasons = Counter()
    for product in list(products):
        suggestion = product.ai_suggestion
        if not is_current(suggestion, context.fingerprint):
            suggestion = suggest_for_product(product, context)
            product.ai_suggestion = suggestion
            product.save(update_fields=["ai_suggestion"])
            remade += 1
            if only_sure and suggestion.get("confidence") != APPROVE_SCOPES[scope]:
                no_longer_sure += 1
                continue
        if suggestion.get("stock_equivalent") == "":
            # Suggested without a factor (finer than its column): a person's
            # to type. Cleared, it would only be made again the same.
            no_factor += 1
            continue
        stock_equivalent = _parse_positive_decimal(str(suggestion.get("stock_equivalent", "")), default=None)
        stock_type = _resolve_suggestion_stock_type(suggestion)

        reason = None
        if stock_type is None:
            reason = "aucun article identifié"
        elif stock_equivalent is None:
            reason = f"facteur de conversion invalide ({suggestion.get('stock_equivalent')!r})"
        elif conversion_refusal(product, stock_type.unit, stock_equivalent):
            reason = f"facteur de conversion hors limites ({suggestion.get('stock_equivalent')!r})"

        if reason:
            skip_reasons[reason] += 1
            # Clear it so the next time the panel is drawn a new suggestion is
            # made (review_panel_context calls apply_rules_to_pending_products,
            # which fills every product whose suggestion is missing or out of
            # date) instead of it being permanently stuck with a bad one.
            product.ai_suggestion = None
            product.save(update_fields=["ai_suggestion"])
            continue

        if stock_type.pk is None:
            # get_or_create, not save(): another tab may have made it since.
            stock_type, _created = StockType.objects.get_or_create(
                name__iexact=stock_type.name,
                defaults={"name": stock_type.name, "unit": stock_type.unit, "category": stock_type.category},
            )
        # product.unit always mirrors stock_type.unit - see assign_product.
        link_product_to_stock_type(product, stock_type, unit=stock_type.unit, stock_equivalent=stock_equivalent)
        approved += 1

    which = "les suggestions sûres (confiance haute)" if only_sure else "les suggestions"
    skipped = sum(skip_reasons.values())
    remade_note = ""
    if remade:
        remade_note = f" {remade} suggestion(s) refaite(s) d'abord : les classements avaient changé."
    if no_longer_sure:
        remade_note += f" {no_longer_sure} laissé(s) à classer, leur suggestion refaite n'étant plus sûre."
    if no_factor:
        remade_note += f" {no_factor} laissé(s) à classer, leur facteur étant à saisir."
    if skipped:
        detail = ", ".join(f"{count} ({reason})" for reason, count in skip_reasons.most_common())
        messages.warning(
            request,
            f"{approved} produit(s) rattaché(s) d'après {which}. {skipped} ignoré(s) : {detail} ; "
            "leur suggestion sera refaite." + remade_note,
        )
    elif approved:
        messages.success(request, f"{approved} produit(s) rattaché(s) automatiquement d'après {which}.{remade_note}")
    elif only_sure:
        messages.info(request, f"Aucune suggestion sûre à approuver pour le moment.{remade_note}")
    else:
        messages.info(request, f"Aucune suggestion à approuver pour le moment.{remade_note}")
    return redirect("inventory:stock_list")


def _parse_positive_decimal(raw: str, default: Decimal) -> Decimal | None:
    """Returns the parsed value, `default` if blank, or None if invalid.

    Valid is what Product.stock_equivalent (10,4) holds, never rounded: a
    wider factor was stored anyway and every read of the product then raised,
    so nothing in the app could correct it again. And no NaN or Infinity,
    which Decimal() takes."""
    raw = raw.strip()
    if not raw:
        return default
    value = read_amount(raw, places=4, digits=10)
    return value if value is not None and value > 0 else None


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
    elif product.stock_type_id is not None:
        # The panel lists only what is left to classify; a panel drawn before
        # another tab classified this one still posts, and moves nothing.
        error = (
            f"« {product.raw_name} » est déjà rangé dans « {product.stock_type.name} » : retirez-le d'abord "
            "ou changez son facteur depuis la page Stock."
        )
    elif stock_equivalent is None:
        error = "« 1 produit = » doit être un nombre positif d'au plus 4 décimales."
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
    else:
        unit = stock_type.unit
    # Asked before the new article is made: a refusal writes nothing.
    refusal = conversion_refusal(product, unit, stock_equivalent)
    if refusal:
        messages.error(request, refusal)
        return _review_panel(request) if _is_htmx(request) else redirect("inventory:stock_list")
    if created:
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


#: « Combler les écarts »: `depuis` is the count the gaps run from - in the
#: address, and posted by the list's forms (not `inventaire`: on « Produits &
#: charges » that names the CLOSING count of a window); `montant` the amount
#: to ring up (TTC), a field the add form posts, never read from an address.
SINCE_PARAM = "depuis"
AMOUNT_PARAM = "montant"


#: The form field carrying the last entry the page showed ("" for none): a
#: second click on « Ajouter » or « Annuler la dernière saisie », or « Effacer
#: la liste » from a tab left open, posts what the list no longer is, and is
#: refused. Checked again inside the transaction that acts (SQLite's
#: IMMEDIATE mode takes the write lock as it opens), so two clicks a second
#: of planning apart cannot both pass.
LAST_SHOWN_PARAM = "derniere"


def _list_moved(request, take) -> bool:
    """Whether the list changed since the page that posted was drawn. A post
    without the field (an old page) is not checked."""
    shown = request.POST.get(LAST_SHOWN_PARAM)
    if shown is None:
        return False
    last = take.gap_fill_entries.order_by("-created_at", "-pk").first()
    return shown != (str(last.pk) if last is not None else "")


#: What a list that moved is told, by what was asked.
LIST_MOVED = "La liste a changé entre-temps : rien n'a été {}, vérifiez-la."
#: Why an amount got no sale, by Plan.reason.
NOTHING_FOR = {
    "below_cheapest": "moins que la recette la moins chère",
    "no_combination": "aucune combinaison de prix ne tombe juste",
}


#: What a refused amount is told, by why it was refused.
AMOUNT_ERRORS = {
    "unreadable": "Montant illisible : tapez par exemple 150,50.",
    "not_positive": "Le montant doit être supérieur à zéro.",
    "too_big": f"{format_money(MAX_AMOUNT, '.0f')} € au plus.",
}


def read_typed_amount(typed: str) -> tuple[Decimal | None, str]:
    """(the amount, "") or (None, why it is refused - a key of AMOUNT_ERRORS)."""
    typed = (typed or "").strip()
    # Its size first, from the unbounded reading: 20 000 000 000 € is too
    # big, not unreadable - read_amount stops at a column's width.
    number = read_number(typed)
    if number is None or AMBIGUOUS_THOUSANDS.fullmatch("".join(typed.split())):
        return None, "unreadable"
    if number <= 0:
        return None, "not_positive"
    if number > MAX_AMOUNT:
        return None, "too_big"
    amount = read_amount(typed)
    if amount is None:
        return None, "unreadable"  # more than two decimals
    return amount, ""


def _gap_filler_url(take) -> str:
    return f"{reverse('inventory:stock_gap_filler')}?{urlencode({SINCE_PARAM: take.pk})}"


def _chosen_take(asked: str, takes) -> StockTake | None:
    return next((take for take in takes if is_id(asked) and take.pk == int(asked)), None)


def unlinked_till_products() -> int:
    """How many till products no recipe claims (and not set aside): what
    « Combler les écarts » warns about."""
    from recipes.models import PosProduct

    return PosProduct.objects.filter(recipe__isnull=True, ignored=False).count()


def stock_gap_filler(request):
    """« Combler les écarts »: the sales to ring up so that every stock gap
    since a count shrinks by about the same share - see inventory/gaps.py and
    inventory/gap_planner.py.

    Amounts are added one after the other (`stock_gap_filler_add`), each
    planned on top of the ones before, until the list is cleared; this page
    shows the list, the last entry to ring up first. A count that cannot be
    read falls back to the latest, never a 500: it arrives from an address."""
    takes = list(StockTake.objects.order_by("-taken_at"))
    said = _messages_by_place(request)
    context = {
        "top_messages": said[""],
        "gap_messages": said[GAPS_MESSAGES],
        "exclusion_messages": said[EXCLUSION_MESSAGES],
        "takes": takes,
        "since_param": SINCE_PARAM,
        "amount_param": AMOUNT_PARAM,
        "last_shown_param": LAST_SHOWN_PARAM,
        "duration_param": DURATION_PARAM,
        "duration_unit_param": DURATION_UNIT_PARAM,
        "since_take_param": SINCE_TAKE_PARAM,
    }
    if not takes:
        # No table and no fold to say them in: every message at the top.
        context["top_messages"] = said[""] + said[GAPS_MESSAGES] + said[EXCLUSION_MESSAGES]
        return render(request, "inventory/stock_gap_filler.html", context)

    asked = request.GET.get(SINCE_PARAM, "")
    take = _chosen_take(asked, takes)
    context["take_not_found"] = bool(asked) and take is None
    take = take or takes[0]

    entries = list(take.gap_fill_entries.all())
    report = gaps_since(take)
    show_list(report, list_counts(entries))
    shown = []
    for entry in entries:
        rows = entry_rows(entry)
        shown.append({"entry": entry, "lines": rows, "till_differs": sum(1 for row in rows if row.till_differs)})
    entered = sum((entry.amount for entry in entries), start=Decimal("0"))
    proposed = sum((entry.total for entry in entries), start=Decimal("0"))
    duration, duration_unit = duration_fields(report.sold_within_months)
    context.update(
        {
            "take": take,
            "report": report,
            "duration": duration,
            "duration_unit": duration_unit,
            "entries": shown,
            "latest": shown[-1] if shown else None,
            "earlier": shown[-2::-1],
            "entered": entered,
            "proposed": proposed,
            "left": entered - proposed,
            "sales": sum(entry.sales for entry in entries),
            "shares": share_summary(report) if entries else None,
            "stale": list_is_stale(report, entries),
            # Till products with no recipe: their sales count as gaps. The
            # page's own count, for whoever opens it - the navigation's badge
            # is counted for the logins given Recettes only (accounts/access.py).
            "unlinked_till_products": unlinked_till_products(),
        }
    )
    return render(request, "inventory/stock_gap_filler.html", context)


def stock_gap_filler_add(request):
    """Add an amount to the list: its sales are planned on top of the
    entries already there, and kept as proposed."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    take = _chosen_take(request.POST.get(SINCE_PARAM, ""), StockTake.objects.order_by("-taken_at"))
    if take is None:
        messages.error(request, "Inventaire introuvable.")
        return redirect("inventory:stock_gap_filler")
    if _list_moved(request, take):
        messages.warning(request, LIST_MOVED.format("ajouté"))
        return redirect(_gap_filler_url(take))
    amount, error = read_typed_amount(request.POST.get(AMOUNT_PARAM, ""))
    if error:
        messages.error(request, AMOUNT_ERRORS[error])
        return redirect(_gap_filler_url(take))

    entries = list(take.gap_fill_entries.all())
    report = gaps_since(take)
    result = fill_gaps(report, amount, list_counts(entries))
    if not result.lines:
        if not report.offers and not report.blocked:
            # No recipe to propose at all: none sold over the menu's window,
            # or those sold pour nothing.
            since = f"depuis le {report.menu_since:%d/%m/%Y}" if report.sold_within_months else "depuis l'inventaire"
            why = f"aucune recette vendue {since}"
            if report.on_menu:
                why += " n'utilise un article"
        else:
            why = NOTHING_FOR.get(result.plan.reason, "plus aucune vente ne tient dans les écarts")
        messages.warning(request, f"Rien pour {group_thousands(amount)} € : {why}.")
        return redirect(_gap_filler_url(take))
    watched_from = sales_watched_from(timezone.localdate())
    with transaction.atomic():
        # A click a second of planning ago, waiting here for this one's write
        # lock, then sees the entry it added.
        if _list_moved(request, take):
            messages.warning(request, LIST_MOVED.format("ajouté"))
            return redirect(_gap_filler_url(take))
        GapFillEntry.objects.create(
            stock_take=take,
            amount=amount,
            total=result.total,
            reason="" if result.plan.reason == "exact" else result.plan.reason,
            lines=entry_lines(result),
            sales_up_to=report.last_sale_day,
            sales_from=watched_from,
            sales_seen=servings_from(watched_from, report.end),
        )
    return redirect(f"{_gap_filler_url(take)}#a-encaisser")


def stock_gap_filler_undo(request):
    """Take the list's last entry back (a mistyped amount)."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    take = _chosen_take(request.POST.get(SINCE_PARAM, ""), StockTake.objects.order_by("-taken_at"))
    if take is None:
        return redirect("inventory:stock_gap_filler")
    with transaction.atomic():
        if _list_moved(request, take):
            messages.warning(request, LIST_MOVED.format("retiré"))
            return redirect(_gap_filler_url(take))
        last = take.gap_fill_entries.order_by("-created_at", "-pk").first()
        if last is not None:
            last.delete()
            messages.success(request, "Dernière saisie retirée.")
    return redirect(_gap_filler_url(take))


def stock_gap_filler_clear(request):
    """Empty the list, to start again."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    take = _chosen_take(request.POST.get(SINCE_PARAM, ""), StockTake.objects.order_by("-taken_at"))
    if take is None:
        return redirect("inventory:stock_gap_filler")
    with transaction.atomic():
        if _list_moved(request, take):
            messages.warning(request, LIST_MOVED.format("effacé"))
            return redirect(_gap_filler_url(take))
        deleted, _per_model = take.gap_fill_entries.all().delete()
    if deleted:
        messages.success(request, "Liste effacée.")
    return redirect(_gap_filler_url(take))


#: The fields the exclusion forms post: an article, a category, an exclusion.
EXCLUDED_ARTICLE_PARAM = "article"
EXCLUDED_CATEGORY_PARAM = "categorie"
EXCLUSION_PARAM = "exclusion"
#: Where an exclusion's message is said: its redirect lands on the gaps table
#: (an article's « Exclure ») or on « Exclus des écarts », screens under the
#: top of the page - so the message is said there, as Marges does.
GAPS_MESSAGES = "ecarts"
EXCLUSION_MESSAGES = "exclusions"


def _messages_by_place(request) -> dict[str, list]:
    """{place: messages} - "" the top of the page, GAPS_MESSAGES above the
    gaps, EXCLUSION_MESSAGES in « Exclus des écarts ». Read once, which also
    marks them said."""
    places: dict[str, list] = {"": [], GAPS_MESSAGES: [], EXCLUSION_MESSAGES: []}
    for message in get_messages(request):
        tags = (message.extra_tags or "").split()
        place = next((tag for tag in (GAPS_MESSAGES, EXCLUSION_MESSAGES) if tag in tags), "")
        places[place].append(message)
    return places


def _back_to_the_gaps(request, anchor: str = ""):
    """The page of the count the form came from, at `anchor` if any."""
    take = _chosen_take(request.POST.get(SINCE_PARAM, ""), StockTake.objects.order_by("-taken_at"))
    if take is None:
        return redirect("inventory:stock_gap_filler")
    return redirect(f"{_gap_filler_url(take)}#{anchor}" if anchor else _gap_filler_url(take))


def _category_words(category: str) -> str:
    return f"« {category} »" if category else "non renseignée"


def stock_gap_filler_exclude(request):
    """Leave an article (`article`, from its row) or a whole category
    (`categorie`, a category some article carries) out of « Combler les
    écarts », for the espace - see GapExclusion."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    article = request.POST.get(EXCLUDED_ARTICLE_PARAM, "")
    category = request.POST.get(EXCLUDED_CATEGORY_PARAM)
    if article:
        stock_type = StockType.objects.filter(pk=int(article)).first() if is_id(article) else None
        if stock_type is None:
            messages.error(request, "Article introuvable : rien n'a été exclu.", extra_tags=GAPS_MESSAGES)
        else:
            GapExclusion.objects.get_or_create(stock_type=stock_type)
            messages.success(
                request, f"« {stock_type.name} » ne compte plus dans les écarts.", extra_tags=GAPS_MESSAGES
            )
        return _back_to_the_gaps(request, "ecarts")
    if category is not None and StockType.objects.filter(category=category).exists():
        GapExclusion.objects.get_or_create(category=category)
        messages.success(
            request, f"Catégorie {_category_words(category)} exclue des écarts.", extra_tags=EXCLUSION_MESSAGES
        )
    else:
        messages.error(request, "Catégorie introuvable : rien n'a été exclu.", extra_tags=EXCLUSION_MESSAGES)
    return _back_to_the_gaps(request, "exclusions")


def stock_gap_filler_include(request):
    """Take an exclusion back (`exclusion`, its id): the category, or the
    article, counts in the gaps again - unless the article's category is
    left out too, which the message then says."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    asked = request.POST.get(EXCLUSION_PARAM, "")
    exclusion = (
        GapExclusion.objects.select_related("stock_type").filter(pk=int(asked)).first() if is_id(asked) else None
    )
    if exclusion is None:
        messages.warning(request, "Cette exclusion n'existe plus : rien n'a changé.", extra_tags=EXCLUSION_MESSAGES)
        return _back_to_the_gaps(request, "exclusions")
    still_covered = (
        exclusion.stock_type_id is not None
        and GapExclusion.objects.filter(category=exclusion.stock_type.category).exists()
    )
    if exclusion.stock_type_id is None:
        said = f"La catégorie {_category_words(exclusion.category)} compte de nouveau dans les écarts."
    elif still_covered:
        said = (
            f"« {exclusion.stock_type.name} » reste exclu : "
            f"sa catégorie {_category_words(exclusion.stock_type.category)} l'est aussi."
        )
    else:
        said = f"« {exclusion.stock_type.name} » compte de nouveau dans les écarts."
    exclusion.delete()
    (messages.warning if still_covered else messages.success)(request, said, extra_tags=EXCLUSION_MESSAGES)
    return _back_to_the_gaps(request, "exclusions")


#: « Recettes vendues il y a moins de … »: how many (`duree`), of which
#: unit (`unite`), or (`depuis_inventaire`, a button of its own) back to the
#: recipes sold since the count - see GapFillSetting.
DURATION_PARAM = "duree"
DURATION_UNIT_PARAM = "unite"
SINCE_TAKE_PARAM = "depuis_inventaire"
#: The units offered, as the form posts them, and the months in one.
DURATION_UNITS = {"mois": 1, "ans": 12}
#: What a refused duration is told, by why it was refused.
DURATION_ERRORS = {
    "unreadable": "Durée illisible : tapez un nombre entier, par exemple 3 mois.",
    "out_of_range": "La durée va d'un mois à 10 ans.",
}


def read_typed_duration(typed: str | None, unit: str) -> tuple[int | None, str]:
    """(the months, "") or (None, why it is refused - a key of
    DURATION_ERRORS). ASCII digits only: « ٣ » is a digit to Python."""
    typed = (typed or "").strip()
    if unit not in DURATION_UNITS or not re.fullmatch(r"[0-9]+", typed):
        return None, "unreadable"
    digits = typed.lstrip("0")
    # Past six digits, out of range whatever they say - and never handed to
    # int(), which refuses a few thousand of them.
    if len(digits) > 6:
        return None, "out_of_range"
    months = int(digits or "0") * DURATION_UNITS[unit]
    if not 1 <= months <= GapFillSetting.MAX_MONTHS:
        return None, "out_of_range"
    return months, ""


def duration_fields(months: int | None) -> tuple[str, str]:
    """What the form shows for a stored duration: whole years in years."""
    if not months:
        return "", "mois"
    if months % 12 == 0:
        return str(months // 12), "ans"
    return str(months), "mois"


def stock_gap_filler_recent(request):
    """Which recipes « Combler les écarts » proposes: those sold over the
    months or years typed (`duree`, `unite`), or those sold since the count
    (`depuis_inventaire`). Kept for the espace - see GapFillSetting."""
    if request.method != "POST":
        return redirect("inventory:stock_gap_filler")
    if request.POST.get(SINCE_TAKE_PARAM):
        months = None
    else:
        months, error = read_typed_duration(
            request.POST.get(DURATION_PARAM, ""), request.POST.get(DURATION_UNIT_PARAM, "")
        )
        if error:
            messages.error(request, DURATION_ERRORS[error])
            return _back_to_the_gaps(request)
    GapFillSetting.objects.update_or_create(pk=GapFillSetting.SINGLETON_PK, defaults={"sold_within_months": months})
    said = "depuis l'inventaire" if months is None else f"il y a {duration_words(months)}"
    messages.success(request, f"Recettes proposées : celles vendues {said}.")
    return _back_to_the_gaps(request)


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
                # Deleted first: a row taken out and the same product typed
                # again in one save met the line still there (one per product).
                for obj in formset.deleted_objects:
                    obj.delete()
                # So are the saved lines moved to another product or article:
                # one put onto the product the next still held (a shift, a
                # swap) met it there. Each is written again under its own pk
                # (save() inserts what its UPDATE no longer finds), its
                # sources rebuilt with it.
                stored = {
                    pk: source
                    for pk, *source in StockTakeLine.objects.filter(
                        pk__in=[line.pk for line in lines if line.pk]
                    ).values_list("pk", "product_id", "stock_type_id")
                }
                moved = [
                    line.pk
                    for line in lines
                    if line.pk and stored.get(line.pk) != [line.product_id, line.stock_type_id]
                ]
                StockTakeLine.objects.filter(pk__in=moved).delete()
                for line in lines:
                    _save_stock_take_line(line)
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
            # The data itself, printed by the template's json_script: a name
            # read off a supplier's document can hold « </script> », which a
            # json.dumps string printed |safe let out of its island (security
            # audit XSS-1, tests/test_json_islands.py).
            "entry_data": entries,
            # A draft kept in the browser may still name an article the old
            # way; the page renames it as it restores it.
            "entry_suffix": STOCK_TYPE_ENTRY_SUFFIX,
            "old_entry_suffixes": list(OLD_STOCK_TYPE_ENTRY_SUFFIXES),
            # What the already-saved lines are worth, so the running total is
            # right the moment the page opens without valuing anything again
            # (a saved line's value is frozen - see StockTake's docstring).
            # None for an employee shown no costs (accounts/access.py): the
            # page prices nothing for him, and prints no value.
            "saved_values": {
                str(line_form.instance.pk): str(line_form.instance.value_ht)
                for line_form in formset.forms
                if line_form.instance.pk and line_form.instance.value_ht is not None
            }
            if access_of(request).sees_costs
            else {},
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
    # What the save's counted_quantity (10,4) and StockTakeLineForm take:
    # Decimal() also reads NaN, Infinity and 1e999999, which were a 500 or a
    # million-digit answer here.
    if not quantity.is_finite() or quantity < 0 or quantity >= Decimal("1000000"):
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
