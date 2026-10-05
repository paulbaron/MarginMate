import json
import math
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.contrib.messages import get_messages
from django.core import signing
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.http import HttpResponse, JsonResponse
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
    plain_number,
    read_amount,
    read_number,
    search_key,
)

from .entries import (
    ARTICLE_KIND,
    ENTRY_MAX,
    ITEMS,
    OLD_STOCK_TYPE_ENTRY_SUFFIXES,
    STOCK_TYPE_ENTRY_SUFFIX,
    EntryResolver,
    is_stock_type_entry,
    item_size,
    product_item_sizes,
)
from .forms import (
    StockTakeForm,
    StockTakeLineFormSet,
    StockTypeForm,
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
    ShoppingExclusion,
    ShoppingList,
    ShoppingListItem,
    ShoppingSetting,
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
    is_discrete_count,
    link_product_to_stock_type,
    merge_stock_types,
    product_base_amount,
    product_counting_ratios,
    refresh_invoice_statuses_for_product,
    unlink_product,
    update_product_conversion,
    value_counted_quantity,
    value_counted_stock_type_quantity,
)
from .shopping import (
    DROPPED_LABEL,
    ELSEWHERE_LAST,
    FEW_PURCHASES,
    NAG_VISITS_HABIT,
    NAG_VISITS_LIFTED,
    PAUSED_LABEL,
    QTY_LAST,
    plan_store,
    rhythms,
    store_choices,
    till_note,
)
from .shopping_data import prepare, settings_from
from .shopping_lists import (
    LABEL_MAX,
    MEASURED,
    NOTE_MAX,
    PACK_RANGE,
    QUANTITY_PLACES,
    RECENT_FINISHED,
    UNIT_SYMBOLS,
    USUAL_WORD,
    YOU,
    AddOutcome,
    Figures,
    ItemOf,
    QuantityTooWide,
    add_item,
    article_item_of,
    card_figures,
    card_labels,
    card_units,
    clean_text,
    counted_unit_words,
    display_names,
    entry_figures,
    entry_units,
    finish,
    line_figures,
    list_aliases,
    list_entries,
    offered_stores,
    open_list_of,
    pack_words,
    quantity_words,
    read_quantity,
    run_order,
    set_ticked,
    store_of,
    usual_figures,
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
    said = _messages_by_place(request, GAP_FILLER_PLACES)
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
GAP_FILLER_PLACES = (GAPS_MESSAGES, EXCLUSION_MESSAGES)


def _messages_by_place(request, places: tuple[str, ...]) -> dict[str, list]:
    """{place: messages} - "" the top of the page, then each of `places`
    (the extra tag a view gave the message) where the page says it: above
    the gaps, in « Exclus des écarts », above « À acheter »… A message
    tagged with none of them goes to the top. Read once, which also marks
    them said."""
    said: dict[str, list] = {"": [], **{place: [] for place in places}}
    for message in get_messages(request):
        tags = (message.extra_tags or "").split()
        said[next((place for place in places if place in tags), "")].append(message)
    return said


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


# ---------------------------------------------------------------------------
# « Prévoir les courses »: inventory/shopping.py plans, inventory/shopping_data.py reads
# ---------------------------------------------------------------------------

#: The store the list is drawn for (`fournisseur`, a supplier's id - the
#: invoice list's own parameter) and how long a purchase made there today
#: must last, until the visit after it (`dans`; none: the store's usual
#: gap). In the address, and carried by every form of the page, so that its
#: answer comes back to the same list.
STORE_PARAM = "fournisseur"
HORIZON_PARAM = "dans"
#: « … jusqu'au passage suivant, dans … jours »: what may be typed, both
#: ends included.
HORIZON_RANGE = (1, 90)
#: From this many visits a store's usual gap is its own (shopping._usual_gap
#: measures the median of its gaps from two of them); under it the gap is
#: shopping.DEFAULT_GAP_DAYS. The menu says « tous les N jours » and the list
#: « (votre rythme ici) » from it only - « (par défaut) » under it.
GAP_MIN_VISITS = 3
#: « Pas ici »: the store an article is no longer proposed at. A field of
#: its own: `fournisseur` only says which list to go back to, and « Ne plus
#: proposer » (everywhere) carries it too.
EXCLUDED_STORE_PARAM = "chez"
#: Where an exclusion form's answer lands (`retour`): above « À acheter »
#: (a line of the list), in « Exclusions » (a fold; the default), or back on
#: « Rythme d'achat ».
LANDING_PARAM = "retour"
LIST_MESSAGES = "liste"
#: « Ajouter » on a line of « Peut-être » and of « Nouveaux ici »: said in
#: that fold, opened for it.
MAYBE_MESSAGES = "peut-etre"
NEW_MESSAGES = "nouveaux"
SETTINGS_MESSAGES = "reglages"
RHYTHM_LANDING = "rythme"
#: Where the list says a form's message (its extra tag), and the anchor its
#: redirect opens the page at: screens under the top, a message said at the
#: top went unseen (the gap filler's lesson).
SHOPPING_ANCHORS = {
    LIST_MESSAGES: "a-acheter",
    MAYBE_MESSAGES: "peut-etre",
    NEW_MESSAGES: "nouveaux-ici",
    SETTINGS_MESSAGES: "reglages",
    EXCLUSION_MESSAGES: "exclusions",
}
SHOPPING_PLACES = tuple(SHOPPING_ANCHORS)
#: The sections whose lines « Ajouter » to the store's shopping list: its
#: answer comes back to the section the line came from.
FORECAST_ADD_PLACES = (LIST_MESSAGES, MAYBE_MESSAGES, NEW_MESSAGES)
#: « Listes de courses »' parameters (the HTTP interface: French). A list by
#: its id (`liste`, a finished one read as it was); the tick page (`mode` =
#: `courses`); the item a form changes (`ligne`); what « Ajouter » posts -
#: a name typed (`nom`) with the unit its quantity counts (`unite`, the
#: stock take's values: « UNIT » items, « L » / « KG » the article's measure;
#: the card posts it too), or the forecast's line (`article`, `produit`,
#: `colis`, never a unit) -, a quantity, a note; the tick's wanted state
#: (`pris`, « 1 » bought, « 0 » not); « Garder les articles non pris »
#: (`garder`). The article reuses EXCLUDED_ARTICLE_PARAM (« article »).
SHOPPING_LIST_PARAM = "liste"
MODE_PARAM = "mode"
RUN_MODE = "courses"
ITEM_PARAM = "ligne"
NAME_PARAM = "nom"
UNIT_PARAM = "unite"
QUANTITY_PARAM = "quantite"
PRODUCT_PARAM = "produit"
PACK_PARAM = "colis"
NOTE_PARAM = "note"
TICKED_PARAM = "pris"
KEEP_PARAM = "garder"
#: The tick block and the card changing an item: where their redirects land
#: (French: a redirect targets them), and the extra tag of the messages said
#: there - under the page's head, where a message at the top went unseen.
RUN_ANCHOR = "courses"
EDIT_ANCHOR = "modifier"
#: « Réglages » (ShoppingSetting): the fields, and the button putting the
#: defaults back.
THRESHOLD_PARAM = "seuil"
MEMORY_PARAM = "memoire"
TILL_PARAM = "caisse"
DEFAULTS_PARAM = "defaut"

HORIZON_ERROR = "Passage suivant : un nombre de jours de {} à {}.".format(*HORIZON_RANGE)
THRESHOLD_ERROR = "Seuil : un nombre entier de {} à {}.".format(*ShoppingSetting.THRESHOLD_RANGE)
MEMORY_ERROR = "Mémoire : un nombre de mois de {} à {}.".format(*ShoppingSetting.MEMORY_RANGE)
#: Between the grey words under « À acheter »: « 2 L · pour 30 jours ».
TO_BUY_NOTE_SEPARATOR = " · "
#: A line's labels, coloured: silent far past its rhythm (amber, to check);
#: « historique court » muted.
BADGE_CLASSES = {DROPPED_LABEL: "status-pending", PAUSED_LABEL: "status-pending"}
QUIET_BADGE_CLASS = "status-ignored"


def read_bounded_number(typed: str | None, low: int, high: int) -> int | None:
    """The whole number typed if it is ASCII digits from `low` to `high`,
    else None. « ٣ » is a digit to Python; and more digits than `high` has
    are out of range whatever they say - never handed to int(), which
    refuses a few thousand of them."""
    typed = (typed or "").strip()
    if not re.fullmatch(r"[0-9]+", typed):
        return None
    digits = typed.lstrip("0") or "0"
    if len(digits) > len(str(high)):
        return None
    number = int(digits)
    return number if low <= number <= high else None


def _whole(value: float) -> int:
    """A count of days or articles as the page says it: rounded half up."""
    return math.floor(value + 0.5)


def _days_ago(day: date, today: date) -> str:
    days = (today - day).days
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    return f"il y a {days} jours"


def _shopping_url(
    store_id: int | None = None, horizon: int | None = None, anchor: str = "", name: str = "inventory:shopping_list"
) -> str:
    query = {}
    if store_id is not None:
        query[STORE_PARAM] = store_id
    if horizon is not None:
        query[HORIZON_PARAM] = horizon
    url = f"{reverse(name)}?{urlencode(query)}" if query else reverse(name)
    return f"{url}#{anchor}" if anchor else url


def _offered_store(asked: str):
    """The supplier `asked` names, if the page offers it: bought at, neither
    a supplier of charges nor the AI pseudo-supplier (shopping_data's
    `offered_stores`, for one id). None otherwise."""
    from invoices.models import Supplier
    from invoices.parsers import LLM_PARSER_KEY

    if not is_id(asked):
        return None
    store = Supplier.objects.filter(pk=int(asked), expenses_only=False).exclude(parser_key=LLM_PARSER_KEY).first()
    if store is None:
        return None
    bought = StockMovement.objects.filter(
        kind=MovementKind.PURCHASE, quantity__gt=0, invoice_line__invoice__supplier=store
    ).exists()
    return store if bought else None


def _shopping_landing(request) -> str:
    """Where an exclusion form's answer lands (`retour`)."""
    asked = request.POST.get(LANDING_PARAM, "")
    return asked if asked in (LIST_MESSAGES, RHYTHM_LANDING) else EXCLUSION_MESSAGES


def _said_at(landing: str) -> str:
    """The extra tag of a message said where `landing` lands: none on
    « Rythme d'achat », which says every message at its top."""
    return "" if landing == RHYTHM_LANDING else landing


def _back_to_the_list(request, landing: str):
    """The list the form came from - its store, its days - opened where its
    message is said; or « Rythme d'achat », with its store."""
    store = _offered_store(request.POST.get(STORE_PARAM, ""))
    store_id = store.pk if store is not None else None
    if landing == RHYTHM_LANDING:
        return redirect(_shopping_url(store_id, name="inventory:shopping_rhythm"))
    horizon = read_bounded_number(request.POST.get(HORIZON_PARAM), *HORIZON_RANGE)
    return redirect(_shopping_url(store_id, horizon, SHOPPING_ANCHORS.get(landing, "")))


def _shopping_params() -> dict:
    """The fields' names, for the templates."""
    return {
        "store_param": STORE_PARAM,
        "horizon_param": HORIZON_PARAM,
        "excluded_store_param": EXCLUDED_STORE_PARAM,
        "landing_param": LANDING_PARAM,
        "article_param": EXCLUDED_ARTICLE_PARAM,
        "category_param": EXCLUDED_CATEGORY_PARAM,
        "exclusion_param": EXCLUSION_PARAM,
        "threshold_param": THRESHOLD_PARAM,
        "memory_param": MEMORY_PARAM,
        "till_param": TILL_PARAM,
        "defaults_param": DEFAULTS_PARAM,
        "list_param": SHOPPING_LIST_PARAM,
        "mode_param": MODE_PARAM,
        "run_mode": RUN_MODE,
        "item_param": ITEM_PARAM,
        "name_param": NAME_PARAM,
        "unit_param": UNIT_PARAM,
        "quantity_param": QUANTITY_PARAM,
        "product_param": PRODUCT_PARAM,
        "pack_param": PACK_PARAM,
        "note_param": NOTE_PARAM,
        "ticked_param": TICKED_PARAM,
        "keep_param": KEEP_PARAM,
    }


def _chosen_store(asked: str, choices):
    return next((choice for choice in choices if is_id(asked) and choice.store_id == int(asked)), None)


def _store_options(choices, prepared, today: date) -> tuple[list, list]:
    """The « Enseigne » menu: (the stores visited lately, « Autres
    enseignes »), each (id, « Grossiste exemple — tous les 7 jours, dernier
    passage il y a 3 jours »). A rhythm only from GAP_MIN_VISITS visits:
    under that, the usual gap is the default, not the store's."""
    regular, rare = [], []
    for choice in choices:
        words = [choice.name]
        if len(prepared.visits.get(choice.store_id, ())) >= GAP_MIN_VISITS:
            gap = _whole(choice.usual_gap)
            words.append("tous les jours" if gap <= 1 else f"tous les {gap} jours")
        said = f"{words[0]} — " + ", ".join([*words[1:], f"dernier passage {_days_ago(choice.last_visit, today)}"])
        (rare if choice.rare else regular).append((choice.store_id, said))
    return regular, rare


def _chance_percent(chance: float) -> int:
    """The chance as a whole percent, rounded down: a line at the threshold
    never reads under it, one under it never reads at it."""
    return math.floor(chance * 100 + 1e-9)


def _line_rows(lines, horizon: int | None, listed: dict | None = None, taken: dict | None = None) -> list[dict]:
    """A section's lines as its table draws them. « Produit » is the store's
    usual product when its count is known, its packs under it (« 3 colis de
    24 »); « À acheter » is ONE number - that product's units, else the
    article's own (« 1.5 kg ») -, a typed horizon's multiplier taken in
    (shopping_lists.line_figures, what a list item would count). Under it,
    in grey: the article's units when the number counts the product, unless
    they would repeat it (an article counted « u. » and the same count), then
    « pour N jours » when a typed horizon multiplied it. « Liste »: what its
    « Ajouter » posts (`add`: those very figures), or the quantity the
    store's open list already holds of the article still to buy (`listed`:
    {article id: its words}); one ticked as bought there is no longer in the
    list - its « Ajouter » puts it back -, said beside the form (`taken`,
    the same shape)."""
    listed = listed or {}
    taken = taken or {}
    rows = []
    for line in lines:
        figures = line_figures(line)
        counts_the_product = line.product_units is not None
        note = []
        if counts_the_product and not (line.unit == UnitChoices.UNIT and line.total_qty == figures.quantity):
            note.append(quantity_words(line.total_qty, line.unit))
        if line.multiplier > 1 and horizon:
            note.append(f"pour {horizon} jours")
        rows.append(
            {
                "line": line,
                "product": line.product_name if counts_the_product else "",
                "packs": pack_words(figures.quantity, figures.pack_size) if counts_the_product else "",
                "to_buy": (
                    plain_number(figures.quantity)
                    if counts_the_product
                    else quantity_words(figures.quantity, figures.unit)
                ),
                "to_buy_note": TO_BUY_NOTE_SEPARATOR.join(note),
                "to_buy_sort": plain_number(figures.quantity),
                "add": {
                    "quantity": plain_number(figures.quantity),
                    "unit": UNIT_SYMBOLS.get(figures.unit, "") if figures.unit else "",
                    "product_id": figures.product_id or "",
                    "pack_size": figures.pack_size or "",
                },
                "listed": listed.get(line.article_id, ""),
                "taken": taken.get(line.article_id, ""),
                "percent": _chance_percent(line.chance),
                "chance_sort": f"{line.chance:.4f}",
                "badges": _badge_words(line.badges),
            }
        )
    return rows


def _badge_words(badges) -> list[tuple[str, str]]:
    """A line's labels with their colours: (word, status-pill class)."""
    return [(word, BADGE_CLASSES.get(word, QUIET_BADGE_CLASS)) for word in badges]


def _due_rows(due_elsewhere) -> list[dict]:
    """« À acheter ailleurs »'s lines, their labels coloured as the list's
    (« en pause » when a recipe still sells a silent article)."""
    return [{"due": due, "badges": _badge_words(due.badges)} for due in due_elsewhere]


def _shopping_exclusions(prepared) -> list[dict]:
    """« Exclusions »: each row with its words, the categories first, then
    the articles by name. An article also left out everywhere or through
    its category says so: taken back alone, it stays out."""
    found = list(ShoppingExclusion.objects.select_related("stock_type", "supplier").order_by())
    categories = {exclusion.category for exclusion in found if exclusion.stock_type_id is None}
    everywhere = {
        exclusion.stock_type_id for exclusion in found if exclusion.stock_type_id and not exclusion.supplier_id
    }
    covers = Counter(article.category or "" for article in prepared.articles.values())
    keyed: list[tuple[tuple[bool, str, str, int], dict]] = []
    for exclusion in found:
        if exclusion.stock_type_id is None:
            category = exclusion.category or ""
            row = {"pk": exclusion.pk, "category": category, "covers": covers[category]}
            keyed.append(((False, category.casefold(), "", exclusion.pk), row))
            continue
        article = exclusion.stock_type
        store = exclusion.supplier.name if exclusion.supplier_id else ""
        also = ""
        if exclusion.supplier_id and article.pk in everywhere:
            also = "exclu partout aussi"
        elif article.category in categories:
            also = "catégorie exclue aussi"
        row = {"pk": exclusion.pk, "category": None, "name": article.name, "store": store, "also": also}
        keyed.append(((True, article.name.casefold(), store.casefold(), exclusion.pk), row))
    keyed.sort(key=lambda item: item[0])
    return [row for _key, row in keyed]


def _categories_to_exclude(prepared) -> list[tuple[str, int]]:
    """The categories of the articles bought, with how many, but those
    already left out: what « Ne jamais proposer la catégorie » offers."""
    bought = Counter(
        prepared.articles[article_id].category or "" for article_id in prepared.buys if article_id in prepared.articles
    )
    return sorted(
        ((category, count) for category, count in bought.items() if category not in prepared.excluded.categories),
        key=lambda item: item[0].casefold(),
    )


def shopping_list(request):
    """« Prévoir les courses »: what the owner usually takes at one store on
    the next visit, from the purchase history - each article already bought
    there with its chance, the list from the threshold on, the rest folded
    (inventory/shopping.py). A page drawn writes nothing. A store or a
    number of days the address cannot give is said and set aside - the
    most visited store, the store's usual gap -, never a 500.

    Each line of « À acheter », « Peut-être » and « Nouveaux ici » goes on
    the store's shopping list from its « Liste » cell (or says it is there,
    still to buy; one ticked as bought is offered again, « Pris (N) »
    beside it); « Tout ajouter (N) » puts every line of « À acheter » not on
    it to buy yet. « Réglages », « Exclusions » and the lines' « Pas ici » /
    « Ne plus proposer » are « Produits & charges »' (`may_tune`): a viewer
    who may not post them is not shown them."""
    said = _messages_by_place(request, SHOPPING_PLACES)
    may_tune = access_of(request).allows("products")
    setting = ShoppingSetting.current()
    settings = settings_from(setting)
    today = timezone.localdate()
    prepared = prepare(today, settings, timezone.now())
    choices = store_choices(prepared)
    context = {
        **_shopping_params(),
        "top_messages": said[""],
        "list_messages": said[LIST_MESSAGES],
        "maybe_messages": said[MAYBE_MESSAGES],
        "new_messages": said[NEW_MESSAGES],
        "settings_messages": said[SETTINGS_MESSAGES],
        "exclusion_messages": said[EXCLUSION_MESSAGES],
        "may_tune": may_tune,
        "setting": setting,
        "threshold_range": ShoppingSetting.THRESHOLD_RANGE,
        "memory_range": ShoppingSetting.MEMORY_RANGE,
        "horizon_range": HORIZON_RANGE,
    }
    if not choices:
        # No list and no fold to say them in: every message at the top.
        context["top_messages"] = [message for place in said.values() for message in place]
        return render(request, "inventory/shopping_list.html", context)

    asked = request.GET.get(STORE_PARAM, "")
    choice = _chosen_store(asked, choices)
    store = choice or choices[0]
    typed = request.GET.get(HORIZON_PARAM, "")
    horizon = read_bounded_number(typed, *HORIZON_RANGE)
    plan = plan_store(prepared, store.store_id, settings, horizon)
    regular, rare = _store_options(choices, prepared, today)
    deposits = _line_rows(plan.deposits, horizon)
    # The store's open list, in one read: what each line already has there -
    # to buy (`listed`), or ticked as bought on a list nobody finished yet
    # (`taken`): that one is no longer in the list, and is offered again.
    # In the words the list says them: « 3 bouteilles de 70 cl », « 2 L ».
    on_the_list = list(
        ShoppingListItem.objects.filter(
            shopping_list__supplier_id=store.store_id, shopping_list__finished_at__isnull=True
        ).values_list("stock_type_id", "quantity", "unit", "item_size", "size_unit", "product_name", "checked_at")
    )
    listed, taken = {}, {}
    for article_id, quantity, unit, size, size_unit, product_name, checked_at in on_the_list:
        if article_id is not None:
            words = quantity_words(quantity, unit, size, size_unit, product_name=product_name)
            (listed if checked_at is None else taken)[article_id] = words
    # A fold not drawn says its messages at the top, never nowhere.
    for drawn, place in (
        (plan.maybe, MAYBE_MESSAGES),
        (plan.new, NEW_MESSAGES),
        (may_tune, SETTINGS_MESSAGES),
        (may_tune, EXCLUSION_MESSAGES),
    ):
        if not drawn:
            context["top_messages"] = [*context["top_messages"], *said[place]]
    context.update(
        {
            "store": store,
            "store_not_found": bool(asked) and choice is None,
            "regular_stores": regular,
            "rare_stores": rare,
            "horizon": horizon,
            "horizon_refused": bool(typed.strip()) and horizon is None,
            "horizon_error": HORIZON_ERROR,
            "horizon_days": _whole(plan.horizon),
            "usual_gap": _whole(plan.usual_gap),
            # Under GAP_MIN_VISITS the days are the default, never « votre rythme ».
            "gap_measured": plan.visits >= GAP_MIN_VISITS,
            "plan": plan,
            "basket": _whole(plan.expected_basket),
            "last_visit_ago": _days_ago(plan.last_visit, today) if plan.last_visit else "",
            # A store whose every article is left out: nothing to plan,
            # though it is no first visit.
            "all_excluded_here": not plan.candidates and bool(prepared.articles_at.get(store.store_id)),
            "to_buy": _line_rows(plan.to_buy, horizon, listed, taken),
            "maybe": _line_rows(plan.maybe, horizon, listed, taken),
            "new": _line_rows(plan.new, horizon, listed, taken),
            "quiet": _line_rows(plan.quiet, horizon),
            "elsewhere": _line_rows(plan.elsewhere, horizon),
            "elsewhere_more": plan.elsewhere_count - len(plan.elsewhere),
            "due_elsewhere": _due_rows(plan.due_elsewhere),
            "deposits": deposits,
            "deposit_categories": [category for category in plan.deposit_hint_categories if category],
            "deposits_without_category": [row for row in deposits if not row["line"].category],
            # Read only for one who may change them (their fold is not drawn otherwise).
            "exclusions": _shopping_exclusions(prepared) if may_tune else [],
            "categories_to_exclude": _categories_to_exclude(prepared) if may_tune else [],
            "list_url": _list_page_url(store.store_id),
            "list_count": len(on_the_list),
            "add_all_count": sum(1 for line in plan.to_buy if line.article_id not in listed),
            # « Comment c'est calculé » says the rules with the module's own figures.
            "rules": {
                "few_purchases": FEW_PURCHASES,
                "nag_lifted": NAG_VISITS_LIFTED,
                "nag_habit": NAG_VISITS_HABIT,
                "elsewhere_last": ELSEWHERE_LAST,
                "quantity_last": QTY_LAST,
            },
        }
    )
    return render(request, "inventory/shopping_list.html", context)


def _rhythm_rows(rows) -> list[dict]:
    """« Rythme d'achat »'s rows, as its table draws them."""
    drawn = []
    for row in rows:
        unit = UNIT_SYMBOLS.get(row.unit, "")
        rhythm = ""
        if row.median_gap is not None:
            gap = _whole(row.median_gap)
            rhythm = "environ tous les jours" if gap <= 1 else f"environ tous les {gap} jours"
        habit = ""
        if row.habit_here is not None:
            hits, seen = row.habit_here
            habit = f"{hits} fois sur {seen} passage{'s' if seen > 1 else ''}"
        drawn.append(
            {
                "row": row,
                "rhythm": rhythm,
                "where": " · ".join(f"{share.name} {_whole(share.share * 100)} %" for share in row.stores),
                "habit": habit,
                "usual": f"{plain_number(row.usual_qty)} {unit}".rstrip(),
                "till": ""
                if row.till_per_week is None
                else f"{plain_number(round(row.till_per_week, 2))} {unit}".rstrip(),
            }
        )
    return drawn


def shopping_rhythm(request):
    """« Rythme d'achat »: every article bought, how regularly and where -
    or only those bought at one store (`fournisseur`), with the habit
    there. The store page's own prepared data and labels (`shopping.rhythms`):
    « plus acheté ? », « en pause » and the next purchase agree with the
    list. « Caisse / semaine » counts the days the till import covers, so a
    lagging import is said, as on the list. A store the address cannot give
    is said, and every store shown. « Ne jamais proposer » is drawn for a
    viewer who may post it (`may_tune`, as on the list)."""
    said = _messages_by_place(request, ())
    settings = settings_from(ShoppingSetting.current())
    prepared = prepare(timezone.localdate(), settings, timezone.now())
    choices = store_choices(prepared)
    asked = request.GET.get(STORE_PARAM, "")
    store = _chosen_store(asked, choices)
    rows = _rhythm_rows(rhythms(prepared, settings, store.store_id if store is not None else None))
    return render(
        request,
        "inventory/shopping_rhythm.html",
        {
            **_shopping_params(),
            "top_messages": said[""],
            "choices": choices,
            "store": store,
            "store_not_found": bool(asked) and store is None,
            "rows": rows,
            "till_note": till_note(prepared, settings),
            "rhythm_landing": RHYTHM_LANDING,
            "may_tune": access_of(request).allows("products"),
        },
    )


def shopping_settings(request):
    """« Réglages » of « Prévoir les courses » (ShoppingSetting): the
    threshold (`seuil`), the habits' memory (`memoire`) and the till
    (`caisse`), or the defaults back (`defaut`). Read as ASCII digits within
    the model's ranges; anything else is refused in French and nothing is
    written. Back to the list, in the fold."""
    if request.method != "POST":
        return redirect("inventory:shopping_list")
    if request.POST.get(DEFAULTS_PARAM):
        ShoppingSetting.objects.filter(pk=ShoppingSetting.SINGLETON_PK).delete()
        messages.success(request, "Réglages remis par défaut.", extra_tags=SETTINGS_MESSAGES)
        return _back_to_the_list(request, SETTINGS_MESSAGES)
    threshold = read_bounded_number(request.POST.get(THRESHOLD_PARAM), *ShoppingSetting.THRESHOLD_RANGE)
    memory = read_bounded_number(request.POST.get(MEMORY_PARAM), *ShoppingSetting.MEMORY_RANGE)
    if threshold is None or memory is None:
        for error, refused in ((THRESHOLD_ERROR, threshold is None), (MEMORY_ERROR, memory is None)):
            if refused:
                messages.error(request, error, extra_tags=SETTINGS_MESSAGES)
        return _back_to_the_list(request, SETTINGS_MESSAGES)
    ShoppingSetting.objects.update_or_create(
        pk=ShoppingSetting.SINGLETON_PK,
        defaults={
            "threshold_percent": threshold,
            "memory_months": memory,
            "use_till": bool(request.POST.get(TILL_PARAM)),
        },
    )
    messages.success(request, "Réglages enregistrés.", extra_tags=SETTINGS_MESSAGES)
    return _back_to_the_list(request, SETTINGS_MESSAGES)


def shopping_exclude(request):
    """Never propose an article again (`article`) - everywhere, or at one
    store only (`chez`, « Pas ici ») - or a whole category (`categorie`, one
    some article carries). See ShoppingExclusion. Left out everywhere, an
    article's « Pas ici » rows go: they say nothing more. Anything that
    cannot be read is one message, and nothing is written."""
    if request.method != "POST":
        return redirect("inventory:shopping_list")
    landing = _shopping_landing(request)
    tag = _said_at(landing)
    article = request.POST.get(EXCLUDED_ARTICLE_PARAM, "")
    category = request.POST.get(EXCLUDED_CATEGORY_PARAM)
    if article:
        stock_type = StockType.objects.filter(pk=int(article)).first() if is_id(article) else None
        # « Pas ici » posts its store, blank or not: never read as everywhere.
        asked_store = request.POST.get(EXCLUDED_STORE_PARAM)
        store = _offered_store(asked_store) if asked_store is not None else None
        if stock_type is None:
            messages.error(request, "Article introuvable : rien n'a été exclu.", extra_tags=tag)
        elif asked_store is not None and store is None:
            messages.error(request, "Enseigne introuvable : rien n'a été exclu.", extra_tags=tag)
        elif store is not None:
            ShoppingExclusion.objects.get_or_create(stock_type=stock_type, supplier=store)
            messages.success(request, f"« {stock_type.name} » ne sera plus proposé chez {store.name}.", extra_tags=tag)
        else:
            with transaction.atomic():
                ShoppingExclusion.objects.get_or_create(stock_type=stock_type, supplier=None)
                ShoppingExclusion.objects.filter(stock_type=stock_type, supplier__isnull=False).delete()
            messages.success(request, f"« {stock_type.name} » ne sera plus proposé.", extra_tags=tag)
        return _back_to_the_list(request, landing)
    if category is not None and StockType.objects.filter(category=category).exists():
        ShoppingExclusion.objects.get_or_create(category=category)
        messages.success(request, f"Catégorie {_category_words(category)} : plus proposée.", extra_tags=tag)
    else:
        messages.error(request, "Catégorie introuvable : rien n'a été exclu.", extra_tags=tag)
    return _back_to_the_list(request, landing)


def shopping_include(request):
    """Take an exclusion back (`exclusion`, its id). An article that stays
    out all the same - left out everywhere beside its « Pas ici », or
    through its category - is said to."""
    if request.method != "POST":
        return redirect("inventory:shopping_list")
    landing = _shopping_landing(request)
    tag = _said_at(landing)
    asked = request.POST.get(EXCLUSION_PARAM, "")
    exclusion = (
        ShoppingExclusion.objects.select_related("stock_type", "supplier").filter(pk=int(asked)).first()
        if is_id(asked)
        else None
    )
    if exclusion is None:
        messages.warning(request, "Cette exclusion n'existe plus : rien n'a changé.", extra_tags=tag)
        return _back_to_the_list(request, landing)
    article = exclusion.stock_type
    stays = ""
    if article is not None:
        if exclusion.supplier_id and ShoppingExclusion.objects.filter(stock_type=article, supplier=None).exists():
            stays = "il l'est aussi partout"
        elif ShoppingExclusion.objects.filter(category=article.category).exists():
            stays = f"sa catégorie {_category_words(article.category)} l'est aussi"
    if article is None:
        said = f"Réinclus : catégorie {_category_words(exclusion.category)}."
    elif stays:
        said = f"« {article.name} » reste exclu : {stays}."
    elif exclusion.supplier_id:
        said = f"Réinclus chez {exclusion.supplier.name} : « {article.name} »."
    else:
        said = f"Réinclus : « {article.name} »."
    exclusion.delete()
    (messages.warning if stays else messages.success)(request, said, extra_tags=tag)
    return _back_to_the_list(request, landing)


# ---------------------------------------------------------------------------
# « Listes de courses » (/courses/listes/, /courses/liste/…): one open list
# per store, shared by every login of the espace, kept read-only once
# finished. Thin views over inventory/shopping_lists.py.
# ---------------------------------------------------------------------------

#: What the list pages and their forms say (tested word for word).
STORE_TO_CHOOSE = "Enseigne introuvable : choisissez-en une."
STORE_NOT_FOUND_ADD = "Enseigne introuvable : rien n'a été ajouté."
STORE_NOT_FOUND_CHANGE = "Enseigne introuvable : rien n'a changé."
LIST_NOT_FOUND = "Liste introuvable."
ARTICLE_NOT_FOUND = "Article introuvable : rien n'a été ajouté."
PRODUCT_NOT_FOUND = "Produit introuvable : rien n'a été ajouté."
NAME_MISSING = "Article : tapez un nom."
NAME_TOO_LONG = f"Article : {LABEL_MAX} caractères au plus."
QUANTITY_REFUSED = f"Quantité : un nombre plus grand que 0, {QUANTITY_PLACES} décimales au plus."
#: A unit (`unite`) the name typed, or the card's item, does not offer - any
#: for a free text, which counts what it names.
UNIT_REFUSED = "Unité : choisissez-en une de la liste proposée."
NOTE_TOO_LONG = f"Note : {NOTE_MAX} caractères au plus."
ADDED = "« {name} » ajouté à la liste ({quantity})."
#: A ticked (bought) item added again: put back to buy (AddOutcome.RELISTED).
RELISTED = "« {name} » remis dans la liste ({quantity})."
ALREADY_LISTED = "« {name} » est déjà dans la liste ({quantity})."
NOTHING_TO_ADD = "Tout est déjà dans la liste."
NOT_IN_LIST = "Cet article n'est plus dans la liste."
LIST_FINISHED = "Cette liste est terminée : rien n'a changé."
CHANGED = "Modifié : « {name} » ({quantity})."
REMOVED = "« {name} » retiré de la liste."
TICK_UNREADABLE = "Rien n'a changé : rechargez la page."
ALREADY_FINISHED = "Ces courses sont déjà terminées."
#: « Courses terminées » posted for a list finished meanwhile (another phone,
#: a tab drawn before, a double submit) while the store has a list in
#: progress: its tick page, saying so - never finished in this name.
ALREADY_FINISHED_NEXT = "Ces courses étaient déjà terminées : voici la liste en cours."
#: The states a tick posts (`pris`): the one WANTED, never a toggle.
TICKED, UNTICKED = "1", "0"


def _added_all_words(added: int, already: int, too_wide: int = 0) -> str:
    """« Tout ajouter »'s message: « 7 articles ajoutés à la liste, 3 y
    étaient déjà. », then how many lines were left out because their figure
    is wider than the list can hold (« 1 non ajouté : quantité trop
    grande. ») - NOTHING_TO_ADD when nothing was added nor left out."""
    if not added and not too_wide:
        return NOTHING_TO_ADD
    if not added:
        said = "Aucun article ajouté"
    else:
        said = "1 article ajouté" if added == 1 else f"{added} articles ajoutés"
    said += " à la liste"
    if already:
        said += f", {already} {'y était' if already == 1 else 'y étaient'} déjà"
    said += "."
    if too_wide:
        said += f" {too_wide} non {'ajouté' if too_wide == 1 else 'ajoutés'} : quantité trop grande."
    return said


def _finished_words(store_name: str, done) -> str:
    """« Courses terminées chez … : 2 / 3 pris. », and how many items were
    kept for the next list (shopping_lists.Finished)."""
    said = f"Courses terminées chez {store_name} : {done.ticked} / {done.total} pris."
    if done.carried:
        said += f" {done.carried} {'gardé' if done.carried == 1 else 'gardés'} pour la prochaine liste."
    return said


def _username(request) -> str:
    """Who writes, as the lists keep it: the login's username."""
    user = getattr(request, "user", None)
    return user.get_username() if user is not None and user.is_authenticated else ""


def _tenant_id(request):
    """The espace the request is bound to (its pk), whose logins the lists
    name (shopping_lists.display_names); None when unbound."""
    tenant = getattr(request, "tenant", None)
    return tenant.pk if tenant is not None else None


def _packs(quantity, unit: str, product_name: str, pack_size) -> str:
    """The packs a quantity makes when it counts the store's product sold in
    packs (« 2 colis de 24 », « à l'unité · colis de 24 »); "" otherwise."""
    return pack_words(quantity, pack_size) if product_name and not unit else ""


def _counted_words(quantity, unit: str, product_name: str, pack_size, item_size=None, size_unit: str = "") -> str:
    """What a message says an item counts: « 2 L », « 3 », « 3 bouteilles de
    70 cl », and for the store's product sold in packs, how many - « 24 · 1
    colis de 24 », « 6 bouteilles de 70 cl · 1 colis de 6 », « 2 · à l'unité
    · colis de 24 » -, so a number is never read as packs."""
    words = quantity_words(quantity, unit, item_size, size_unit, product_name=product_name)
    packs = _packs(quantity, unit, product_name, pack_size)
    return f"{words} · {packs}" if packs else words


def _item_words(item) -> str:
    """`_counted_words` of an item as stored."""
    return _counted_words(item.quantity, item.unit, item.product_name, item.pack_size, item.item_size, item.size_unit)


def _list_page_url(store_id: int, *, run: bool = False, item: int | None = None, anchor: str = "") -> str:
    """A store's list page - to prepare, or to tick (`run`) -, the card of
    `item` open, at `anchor`."""
    query: dict = {STORE_PARAM: store_id}
    if run:
        query[MODE_PARAM] = RUN_MODE
    if item is not None:
        query[ITEM_PARAM] = item
    url = f"{reverse('inventory:shopping_list_page')}?{urlencode(query)}"
    return f"{url}#{anchor}" if anchor else url


def _items_of(shopping_list) -> list:
    """A list's items in the order they were added, their articles read
    with them; none for no list."""
    if shopping_list is None:
        return []
    return list(shopping_list.items.select_related("stock_type").order_by("added_at", "pk"))


def _card_counts(item) -> str:
    """What the card's number counts, in grey beside it: for an article's
    item the store's product it names (its select says the unit); for a free
    text still counting something - an article deleted (SET_NULL), or merged
    beside a twin counting something else - that unit, « L » or « bouteilles
    de 70 cl », then the product after « · » (it has no select to say it). A
    free text typed counts what it names: its product, if any, alone."""
    if item.stock_type_id is not None:
        return item.product_name
    said = counted_unit_words(item.unit, item.item_size, item.size_unit, item.product_name)
    return " · ".join(words for words in (said, item.product_name) if words)


def _item_rows(items, store_id: int) -> list[dict]:
    """Items as the pages draw them: the quantity in words - with what it
    counts: « 2 L », « 3 bouteilles de 70 cl » - and its bare number (what it
    sorts by, what its card's field holds); what that number counts, in
    grey beside the card's unit (`_card_counts`), and the packs the store's
    product makes; the address of its card."""
    return [
        {
            "item": item,
            "quantity": quantity_words(
                item.quantity, item.unit, item.item_size, item.size_unit, product_name=item.product_name
            ),
            "plain": plain_number(item.quantity),
            "counts": _card_counts(item),
            "packs": _packs(item.quantity, item.unit, item.product_name, item.pack_size),
            "edit_url": _list_page_url(store_id, item=item.pk, anchor=EDIT_ANCHOR),
        }
        for item in items
    ]


def _ticked_count(items) -> int:
    return sum(1 for item in items if item.checked_at is not None)


def _item_now(item, store, today: date):
    """What one item (« une bouteille ») of `item`'s article is at `store`
    now (shopping_lists.article_item_of, through `usual_figures` - its
    usual purchase here); None for a free text, or an article whose unit
    already counts pieces. Read for the card only: up to five queries."""
    article = item.stock_type
    if article is None or article.unit not in MEASURED:
        return None
    return article_item_of(article, usual_figures(today, store, article))


def _own_product(item, store, item_now) -> ItemOf | None:
    """The product `item` names found again at `store` under its article,
    with the size of one (entries.item_size) - read only where the card's
    items option would count it (shopping_lists.card_item: the item counts a
    measure and names a product `item_now` does not), so that option says,
    and stores, that product's bottles rather than a bare number. None when
    not needed or not found: up to two queries."""
    article = item.stock_type
    if article is None or (item.unit or ITEMS) == ITEMS or not item.product_name:
        return None
    if item_now is not None and item_now.product_name == item.product_name:
        return None
    product = (
        Product.objects.filter(supplier=store, raw_name=item.product_name, stock_type=article)
        .order_by("pk")
        .only("id", "raw_name", "unit", "stock_equivalent")
        .first()
    )
    if product is None:
        return None
    size = item_size(product, product_counting_ratios([product.pk]))
    return ItemOf(size, article.unit if size is not None else "", product.pk, product.raw_name, None)


def _card_units(item, store, today: date) -> list[tuple[str, str, bool]]:
    """The card's unit select for `item` (shopping_lists.card_units /
    card_labels, through card_item: what its items option counts, the
    item's own product found again when it names one the store's usual does
    not): (value, label, selected), its present terms selected; none for a
    free text, which keeps its number."""
    item_now = _item_now(item, store, today)
    own = _own_product(item, store, item_now)
    units = card_units(item, item_now, own)
    if units is None:
        return []
    return [(value, label, value == units.default) for value, label in card_labels(item, units, item_now, own)]


def _item_of(asked, store):
    """The item `asked` (an id read from a form) of one of the store's
    lists, open or finished, with its list and article; None otherwise - an
    item of another store's list included."""
    if not is_id(asked):
        return None
    return (
        ShoppingListItem.objects.select_related("shopping_list", "stock_type")
        .filter(pk=int(asked), shopping_list__supplier=store)
        .first()
    )


def _gone_or_finished(item) -> str:
    """Why a write filtered on an open list found nothing: its list was
    finished meanwhile, or the item went."""
    return LIST_FINISHED if ShoppingListItem.objects.filter(pk=item.pk).exists() else NOT_IN_LIST


def _run_context(store, notices=()) -> dict:
    """The tick block (_shopping_run.html): the store's open list as it
    stands now - after another phone finished, the one carried over -,
    unticked first, and what the last tick could not do (`notices`)."""
    shopping_list = open_list_of(store.pk)
    items = run_order(_items_of(shopping_list))
    return {
        **_shopping_params(),
        "store": store,
        "shopping_list": shopping_list,
        "items": _item_rows(items, store.pk),
        "ticked": _ticked_count(items),
        "total": len(items),
        "notices": list(notices),
        "edit_url": _list_page_url(store.pk),
    }


def shopping_lists(request):
    """« Listes de courses »: the lists in progress, one per store - an open
    list holding an item: one emptied by « Retirer » is none -, the store
    menu opening one (a GET: a list is made by its first item), and the last
    RECENT_FINISHED lists finished, read-only, with who finished them, never
    an address (display_names). Writes nothing; the same queries whatever
    the lists hold."""
    said = _messages_by_place(request, ())
    lists = ShoppingList.objects.select_related("supplier").annotate(
        total=Count("items"), ticked=Count("items", filter=Q(items__checked_at__isnull=False))
    )
    open_lists = sorted(
        lists.filter(finished_at__isnull=True, total__gt=0),
        key=lambda found: (search_key(found.supplier.name), found.pk),
    )
    finished = list(lists.filter(finished_at__isnull=False).order_by("-finished_at", "-pk")[:RECENT_FINISHED])
    names = display_names([found.finished_by for found in finished], _username(request), _tenant_id(request))
    return render(
        request,
        "inventory/shopping_lists.html",
        {
            **_shopping_params(),
            "top_messages": said[""],
            "open_lists": [
                {
                    "list": found,
                    "url": _list_page_url(found.supplier_id),
                    "run_url": _list_page_url(found.supplier_id, run=True),
                }
                for found in open_lists
            ],
            "finished_lists": [{"list": found, "by": names.get(found.finished_by, "")} for found in finished],
            "stores": sorted(offered_stores(), key=lambda store: (search_key(store.name), store.pk)),
        },
    )


def shopping_list_page(request):
    """One store's shopping list (`fournisseur`): to prepare - add an
    article, one of the store's products or a free text, in the unit
    chosen; change a quantity, its unit or a note on the card of `ligne`;
    remove one - or, `mode=courses`, to tick in the store. A list by its id
    (`liste`): an open one goes to its store's address, a finished one is
    read as it was. A GET writes nothing: a list is made by its first item.
    A store or a list the address cannot give: the lists' page, saying so.

    To prepare, the add form's menu (`entries`, its datalist) and what its
    unit select reads (`entry_data`, a json_script island that
    static/js/entry_units.js fills it from) are shopping_lists.list_entries' -
    the chain the add itself goes through -, and the names the add also
    accepts (`entry_aliases`, shopping_lists.list_aliases: the resolver's
    own answers) a second island; the card's select is drawn here, for its
    one item."""
    asked_list = request.GET.get(SHOPPING_LIST_PARAM, "")
    run = request.GET.get(MODE_PARAM) == RUN_MODE
    if asked_list:
        found = (
            ShoppingList.objects.select_related("supplier").filter(pk=int(asked_list)).first()
            if is_id(asked_list)
            else None
        )
        if found is None:
            messages.warning(request, LIST_NOT_FOUND)
            return redirect("inventory:shopping_lists")
        if found.is_open:
            return redirect(_list_page_url(found.supplier_id, run=run))
        return _finished_list_page(request, found)
    store = store_of(request.GET.get(STORE_PARAM, ""))
    if store is None:
        messages.warning(request, STORE_TO_CHOOSE)
        return redirect("inventory:shopping_lists")
    if run:
        # A tick refused without JavaScript is said in the block it lands on.
        said = _messages_by_place(request, (RUN_ANCHOR,))
        context = _run_context(store, [str(message) for message in said[RUN_ANCHOR]])
        context.update({"run": True, "top_messages": said[""]})
        return render(request, "inventory/shopping_list_page.html", context)
    shopping_list = open_list_of(store.pk)
    items = _items_of(shopping_list)
    rows = _item_rows(items, store.pk)
    asked_item = request.GET.get(ITEM_PARAM, "")
    editing = next((row for row in rows if is_id(asked_item) and row["item"].pk == int(asked_item)), None)
    # The card's refusals are said in it; with no card, at the top.
    said = _messages_by_place(request, (EDIT_ANCHOR,) if editing else ())
    today = timezone.localdate()
    if editing is not None:
        editing = {**editing, "units": _card_units(editing["item"], store, today)}
    menu = list_entries(today, store)
    aliases = list_aliases(store, menu)
    return render(
        request,
        "inventory/shopping_list_page.html",
        {
            **_shopping_params(),
            "top_messages": said[""],
            "edit_messages": said.get(EDIT_ANCHOR, []),
            "store": store,
            "shopping_list": shopping_list,
            "rows": rows,
            "ticked": _ticked_count(items),
            "total": len(items),
            "editing": editing,
            "item_missing": bool(asked_item) and editing is None,
            "not_in_list": NOT_IN_LIST,
            "entries": [entry.name for entry in menu],
            # A dict: json_script writes it (never a JSON string printed |safe).
            "entry_data": {entry.name: entry.as_data() for entry in menu},
            # The names the server also accepts, for the select to follow them.
            "entry_aliases": aliases,
            "usual_word": USUAL_WORD,
            # A menu name is never cut: the longest one the vocabulary makes.
            "entry_max": ENTRY_MAX,
            "note_max": NOTE_MAX,
            "edit_url": _list_page_url(store.pk),
            "run_url": _list_page_url(store.pk, run=True),
        },
    )


def _finished_list_page(request, shopping_list):
    """A finished list, read as it was: who finished it (never an address),
    when, what was bought. No form."""
    said = _messages_by_place(request, ())
    items = _items_of(shopping_list)
    by = display_names([shopping_list.finished_by], _username(request), _tenant_id(request)).get(
        shopping_list.finished_by, ""
    )
    return render(
        request,
        "inventory/shopping_list_page.html",
        {
            **_shopping_params(),
            "top_messages": said[""],
            "finished": True,
            "store": shopping_list.supplier,
            "shopping_list": shopping_list,
            "rows": _item_rows(items, shopping_list.supplier_id),
            "ticked": _ticked_count(items),
            "total": len(items),
            # In a sentence: « … par vous ».
            "finished_by": "vous" if by == YOU else by,
        },
    )


def _read_line(post, store, refused: list) -> tuple | None:
    """« Ajouter » on a forecast line: (article, label, figures) - the
    quantity typed, counting the product posted (one of that article's at
    `store`: another store's is refused; its pack size a hint, never a
    refusal; the size of one item kept, in the article's unit, when it has
    one - entries.item_size) or, with none, the article's own unit. `unite`
    is never read: the line's number counts what its product, or its
    absence, says. With no store read, the product is not looked up: the
    store's refusal alone is said. None when something is refused (said in
    `refused`)."""
    asked = post.get(EXCLUDED_ARTICLE_PARAM, "")
    article = StockType.objects.filter(pk=int(asked)).first() if is_id(asked) else None
    if article is None:
        refused.append(ARTICLE_NOT_FOUND)
    asked_product = post.get(PRODUCT_PARAM, "")
    product = None
    if article is not None and store is not None and asked_product:
        product = (
            Product.objects.filter(pk=int(asked_product), stock_type=article, supplier=store).first()
            if is_id(asked_product)
            else None
        )
        if product is None:
            refused.append(PRODUCT_NOT_FOUND)
    quantity = read_quantity(post.get(QUANTITY_PARAM, ""))
    if quantity is None:
        refused.append(QUANTITY_REFUSED)
    if article is None or store is None or quantity is None or (asked_product and product is None):
        return None
    if product is None:
        return article, article.name, Figures(quantity, article.unit, None, "", None)
    pack = read_bounded_number(post.get(PACK_PARAM), *PACK_RANGE)
    size = item_size(product, product_counting_ratios([product.pk]))
    figures = Figures(quantity, "", product.pk, product.raw_name, pack, size, article.unit if size is not None else "")
    return article, article.name, figures


def _read_entry(post, store, refused: list) -> tuple | None:
    """« Ajouter » on the list page: (article, label, figures) for the name
    typed (`nom`), in the unit chosen (`unite`; empty or not posted: the
    entry's own default - an old page, no JavaScript).

    The name goes through the stock take's resolver, scoped to the store
    (entries.EntryResolver, forgiving): one of the store's products by its
    menu name or its own, an article by its menu name or its name typed
    otherwise - an article always before a product reading alike -, else a
    free text, kept as typed. A product adds its article: the product only
    says what the number counts. What each unit stores is
    shopping_lists.entry_figures', the chain the page's menu goes through
    (list_entries): the number typed counts the unit chosen, never
    converted; nothing typed, the store's usual figure in that unit.

    A unit the entry does not offer - any for a free text - is refused
    (UNIT_REFUSED), judged once the name reads, the quantity refused or
    not. The name's length is judged by what is stored (NAME_TOO_LONG): a
    text longer than any menu name can be (entries.ENTRY_MAX) before it is
    read; a free text, stored as typed, over LABEL_MAX - an entry stores its
    article's name, which always fits. None when something is refused (said
    in `refused`)."""
    names_at = len(refused)
    text = clean_text(post.get(NAME_PARAM, ""))
    if not text:
        refused.append(NAME_MISSING)
    elif len(text) > ENTRY_MAX:
        # Longer than any name the menu offers: nothing it could name.
        refused.append(NAME_TOO_LONG)
    typed = post.get(QUANTITY_PARAM, "")
    quantity = None
    if typed.strip():
        quantity = read_quantity(typed)
        if quantity is None:
            refused.append(QUANTITY_REFUSED)
    asked = post.get(UNIT_PARAM, "")
    if store is None or NAME_MISSING in refused or NAME_TOO_LONG in refused:
        return None
    entry = EntryResolver(supplier_id=store.pk).resolve(text, forgiving=True)
    if entry is None:
        if len(text) > LABEL_MAX:
            # A free text is stored as typed; an entry stores its article's
            # name, which always fits. Said where the name's refusals are.
            refused.insert(names_at, NAME_TOO_LONG)
            return None
        if asked:
            refused.append(UNIT_REFUSED)
        if refused:
            return None
        return None, text, entry_figures(None, "", quantity, usual=None, item=None, product_size=None)
    article = entry.article
    usual = usual_figures(timezone.localdate(), store, article)
    item = article_item_of(article, usual) if entry.kind == ARTICLE_KIND else None
    product_size, discrete = None, False
    if entry.product is not None:
        ratios = product_counting_ratios([entry.product.pk])
        product_size = item_size(entry.product, ratios)
        discrete = is_discrete_count(ratios, entry.product.pk)
    units = entry_units(entry, usual=usual, item=item, discrete=discrete)
    unit = asked or units.default
    if unit not in units.choices:
        refused.append(UNIT_REFUSED)
    if refused:
        return None
    figures = entry_figures(entry, unit, quantity, usual=usual, item=item, product_size=product_size)
    return article, article.name, figures


def shopping_list_add(request):
    """« Ajouter »: one item on the store's open list, made by this first
    item. From the list page a name typed in the unit chosen (`nom`,
    `unite`, _read_entry), back to the list; from « Prévoir les courses » a
    line (`article`, `produit`, `colis`, the quantity its cell shows,
    _read_line), back to the section it came from (`retour`), its days
    kept. Every refusal is said, and nothing is written - a figure wider
    than the list holds (a usual purchase misread) included; an item still
    to buy is said already there, unchanged - a double submit adds nothing
    -, and one ticked as bought is put back to buy with the figures posted
    (« remis dans la liste »). The message says what the number counts
    (_counted_words: « 3 bouteilles de 70 cl · à l'unité · colis de 6 »)."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    landing = request.POST.get(LANDING_PARAM, "")
    from_forecast = landing in FORECAST_ADD_PLACES
    tag = landing if from_forecast else ""
    refused: list[str] = []
    store = store_of(request.POST.get(STORE_PARAM, ""))
    if store is None:
        refused.append(STORE_NOT_FOUND_ADD)
    if request.POST.get(EXCLUDED_ARTICLE_PARAM, ""):
        read = _read_line(request.POST, store, refused)
    else:
        read = _read_entry(request.POST, store, refused)
    note = clean_text(request.POST.get(NOTE_PARAM, ""))
    if len(note) > NOTE_MAX:
        refused.append(NOTE_TOO_LONG)
    if refused or read is None or store is None:
        for said in refused:
            messages.error(request, said, extra_tags=tag)
    else:
        article, label, figures = read
        try:
            item, outcome = add_item(
                store, by=_username(request), label=label, figures=figures, stock_type=article, note=note
            )
        except QuantityTooWide as refusal:
            messages.error(request, str(refusal), extra_tags=tag)
        else:
            words = {"name": item.name, "quantity": _item_words(item)}
            if outcome is AddOutcome.ADDED:
                messages.success(request, ADDED.format(**words), extra_tags=tag)
            elif outcome is AddOutcome.RELISTED:
                messages.success(request, RELISTED.format(**words), extra_tags=tag)
            else:
                messages.warning(request, ALREADY_LISTED.format(**words), extra_tags=tag)
    if from_forecast:
        return _back_to_the_list(request, landing)
    return redirect(_list_page_url(store.pk) if store is not None else "inventory:shopping_lists")


def shopping_list_add_all(request):
    """« Tout ajouter (N) »: every line of « À acheter » not on the store's
    open list to buy yet, with the figures the page shows - the plan worked
    out again, as the page does it, the typed days (`dans`) included -, a
    line counting the store's product keeping the size of one item
    (entries.product_item_sizes, two queries), as « Ajouter » does. An item
    ticked as bought there is put back to buy and counted as added; a line
    whose figure the list cannot hold is left out and counted. Said above
    « À acheter »; a second submit adds nothing."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    store = _offered_store(request.POST.get(STORE_PARAM, ""))
    if store is None:
        messages.error(request, STORE_NOT_FOUND_ADD, extra_tags=LIST_MESSAGES)
        return _back_to_the_list(request, LIST_MESSAGES)
    horizon = read_bounded_number(request.POST.get(HORIZON_PARAM), *HORIZON_RANGE)
    settings = settings_from(ShoppingSetting.current())
    plan = plan_store(prepare(timezone.localdate(), settings, timezone.now()), store.pk, settings, horizon)
    # Still to buy on the open list: a ticked item is no longer in it.
    listed = set(
        ShoppingListItem.objects.filter(
            shopping_list__supplier=store,
            shopping_list__finished_at__isnull=True,
            stock_type__isnull=False,
            checked_at__isnull=True,
        ).values_list("stock_type_id", flat=True)
    )
    articles = StockType.objects.in_bulk([line.article_id for line in plan.to_buy])
    sizes = product_item_sizes({line.product_id for line in plan.to_buy if line.product_id})
    by = _username(request)
    added = already = too_wide = 0
    with transaction.atomic():
        for line in plan.to_buy:
            article = articles.get(line.article_id)
            if article is None:
                continue
            if article.pk in listed:
                already += 1
                continue
            try:
                _item, outcome = add_item(
                    store, by=by, label=article.name, figures=line_figures(line, sizes), stock_type=article
                )
            except QuantityTooWide:
                # Refused before anything is read or written: the others go on.
                too_wide += 1
                continue
            if outcome is AddOutcome.ALREADY:
                already += 1
            else:
                added += 1
    said = _added_all_words(added, already, too_wide)
    if too_wide:
        messages.warning(request, said, extra_tags=LIST_MESSAGES)
    else:
        (messages.success if added else messages.info)(request, said, extra_tags=LIST_MESSAGES)
    return _back_to_the_list(request, LIST_MESSAGES)


def shopping_list_item_edit(request):
    """The card's « Enregistrer »: an item's quantity, the unit it counts
    (`unite`, one of the card's select: shopping_lists.card_units) and its
    note (which may be emptied), on an open list only - ONE write filtered
    on one, so a list finished meanwhile is never written. The number is
    never converted: it counts the unit chosen (shopping_lists.card_figures
    - from bottles to litres the product's name is kept, its packs and size
    cleared; from litres to bottles, those of the article's usual purchase
    here - or, when the item names another product, that product's, found
    again at the store, else a bare number of it: what the card's label
    said, shopping_lists.card_item). No unit posted (an old page): the item
    keeps its own. A refusal -
    a unit the card does not offer, any for a free text, included - is said
    in the card, drawn again, and nothing is written. The message says what
    the number counts (_counted_words)."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    store = store_of(request.POST.get(STORE_PARAM, ""))
    if store is None:
        messages.warning(request, STORE_NOT_FOUND_CHANGE)
        return redirect("inventory:shopping_lists")
    item = _item_of(request.POST.get(ITEM_PARAM, ""), store)
    if item is None or not item.shopping_list.is_open:
        messages.warning(request, NOT_IN_LIST if item is None else LIST_FINISHED)
        return redirect(_list_page_url(store.pk))
    quantity = read_quantity(request.POST.get(QUANTITY_PARAM, ""))
    note = clean_text(request.POST.get(NOTE_PARAM, ""))
    asked = request.POST.get(UNIT_PARAM, "")
    # What one item of its article is now, and the item's own product found
    # again: read only to count items again from a measure (the units
    # offered and what they store, shopping_lists.card_item).
    item_now = own = None
    if asked and asked != (item.unit or ITEMS):
        item_now = _item_now(item, store, timezone.localdate())
        own = _own_product(item, store, item_now)
    units = card_units(item, item_now, own)
    unit_refused = bool(asked) and (units is None or asked not in units.choices)
    refused = [
        said
        for said, wrong in (
            (QUANTITY_REFUSED, quantity is None),
            (UNIT_REFUSED, unit_refused),
            (NOTE_TOO_LONG, len(note) > NOTE_MAX),
        )
        if wrong
    ]
    if refused or quantity is None:
        for said in refused:
            messages.error(request, said, extra_tags=EDIT_ANCHOR)
        return redirect(_list_page_url(store.pk, item=item.pk, anchor=EDIT_ANCHOR))
    figures = card_figures(item, asked or None, quantity, item_now, own)
    changed = ShoppingListItem.objects.filter(pk=item.pk, shopping_list__finished_at__isnull=True).update(
        quantity=figures.quantity,
        note=note,
        unit=figures.unit,
        product_name=figures.product_name,
        pack_size=figures.pack_size,
        item_size=figures.item_size,
        size_unit=figures.size_unit,
    )
    if changed:
        counted = _counted_words(
            figures.quantity,
            figures.unit,
            figures.product_name,
            figures.pack_size,
            figures.item_size,
            figures.size_unit,
        )
        messages.success(request, CHANGED.format(name=item.name, quantity=counted))
    else:
        messages.warning(request, _gone_or_finished(item))
    return redirect(_list_page_url(store.pk))


def shopping_list_item_delete(request):
    """« Retirer »: an item off an open list, without asking - adding it
    back is one form."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    store = store_of(request.POST.get(STORE_PARAM, ""))
    if store is None:
        messages.warning(request, STORE_NOT_FOUND_CHANGE)
        return redirect("inventory:shopping_lists")
    item = _item_of(request.POST.get(ITEM_PARAM, ""), store)
    if item is None:
        messages.warning(request, NOT_IN_LIST)
        return redirect(_list_page_url(store.pk))
    deleted, _counts = ShoppingListItem.objects.filter(pk=item.pk, shopping_list__finished_at__isnull=True).delete()
    if deleted:
        messages.success(request, REMOVED.format(name=item.name))
    else:
        messages.warning(request, _gone_or_finished(item))
    return redirect(_list_page_url(store.pk))


def shopping_list_item_tick(request):
    """A tick in the store: the item bought (`pris` « 1 ») or not (« 0 ») -
    the state WANTED, never a toggle, so two phones ticking two items both
    land and a double tap changes nothing more. htmx gets the tick block
    back (the store's open list as it stands, what could not be done said
    in it, nothing stored for the next page) and, out of band, the list
    « Courses terminées » is to post - the one the block shows, or the form
    taken out when it shows none (`oob`, _shopping_run.html); without
    JavaScript, the tick page at the block."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    htmx = request.headers.get("HX-Request") == "true"
    store = store_of(request.POST.get(STORE_PARAM, ""))
    if store is None:
        if htmx:
            response = HttpResponse()
            response["HX-Redirect"] = reverse("inventory:shopping_lists")
            return response
        messages.warning(request, STORE_NOT_FOUND_CHANGE)
        return redirect("inventory:shopping_lists")
    item = _item_of(request.POST.get(ITEM_PARAM, ""), store)
    wanted = request.POST.get(TICKED_PARAM, "")
    notice = ""
    if item is None:
        notice = NOT_IN_LIST
    elif not item.shopping_list.is_open:
        notice = LIST_FINISHED
    elif wanted not in (TICKED, UNTICKED):
        notice = TICK_UNREADABLE
    elif not set_ticked(item.pk, wanted == TICKED, _username(request), timezone.now()):
        notice = _gone_or_finished(item)
    if htmx:
        context = {**_run_context(store, [notice] if notice else []), "oob": True}
        return render(request, "inventory/_shopping_run.html", context)
    if notice:
        messages.warning(request, notice, extra_tags=RUN_ANCHOR)
    return redirect(_list_page_url(store.pk, run=True, anchor=RUN_ANCHOR))


def shopping_list_finish(request):
    """« Courses terminées »: the list (`liste`) closed once - a double
    submit carries nothing twice - and, with « Garder… » (`garder`), what
    was not ticked copied to the store's next open list. To the lists'
    page. A list finished already - a double submit, another phone, a tab
    drawn before - is never taken for another: while the store has a list
    in progress, its tick page, saying so (this phone may never have seen
    that list); else the lists, saying it was finished."""
    if request.method != "POST":
        return redirect("inventory:shopping_lists")
    asked = request.POST.get(SHOPPING_LIST_PARAM, "")
    shopping_list = (
        ShoppingList.objects.select_related("supplier").filter(pk=int(asked)).first() if is_id(asked) else None
    )
    if shopping_list is None:
        messages.error(request, LIST_NOT_FOUND)
        return redirect("inventory:shopping_lists")
    done = finish(shopping_list, keep=bool(request.POST.get(KEEP_PARAM)), by=_username(request), now=timezone.now())
    if done is not None:
        messages.success(request, _finished_words(shopping_list.supplier.name, done))
        return redirect("inventory:shopping_lists")
    in_progress = open_list_of(shopping_list.supplier_id)
    if in_progress is not None and in_progress.items.exists():
        messages.warning(request, ALREADY_FINISHED_NEXT, extra_tags=RUN_ANCHOR)
        return redirect(_list_page_url(shopping_list.supplier_id, run=True, anchor=RUN_ANCHOR))
    messages.info(request, ALREADY_FINISHED)
    return redirect("inventory:shopping_lists")
