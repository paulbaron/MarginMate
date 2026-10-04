"""« Prévoir les courses »' read of the database: inventory/shopping_data.py.

What the page promises at that level, pinned on worked examples:

* the queries are the same however long the history is - with the till on
  and off - and with the till off not one sales table is read;
* a purchase is dated by its movement's own `occurred_on`, else by its
  invoice's date, never by when it was classified; an undated invoice and
  one dated after today are left out; a return is kept apart;
* the suppliers of charges and the AI pseudo-supplier are never offered,
  while what was bought there still counts;
* the till is read over the year up to its import's coverage, attributed
  once by the engine (`variance.attribute_sales`) and spread per day so
  that the days add back up to the engine's own figures, an « OU » included;
* the till's first day is the first day its import recorded a sale: a sale
  typed by hand or a sale document older than that is consumption, never
  the till's start;
* a small history read from the database plans the expected list;
* and one article's usual purchase at one store (`usual_purchase_at`, the
  scan narrowed to them) is the page's own, in two queries at most.

Invented data throughout: every store, article, product, recipe, price and
quantity is made up for these tests, and the days are fixed invented days.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from inventory import shopping, shopping_data
from inventory.models import MovementKind, ShoppingExclusion, ShoppingSetting, UnitChoices
from inventory.shopping import (
    CALENDAR_CLOCK,
    TILL_CLOCK,
    Exclusions,
    Settings,
    plan_store,
    score_article,
    store_choices,
)
from inventory.shopping_data import (
    DailyUse,
    article_infos,
    exclusions,
    offered_stores,
    prepare,
    purchase_rows,
    read_till,
    settings_from,
)
from inventory.variance import attribute_sales, read_sales
from invoices.models import GatherCoverage, Invoice
from invoices.parsers import LLM_PARSER_KEY
from recipes import auto_sales, sales_sources
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
    variation_scope,
)
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_type,
    make_supplier,
)
from tests.test_views_smoke import SHOPPING_BEER_PRODUCT, make_shopping_history

#: The page's day, and a moment of it past the night's end (06:00 by
#: default): the last complete till day is the day before.
TODAY = date(2026, 3, 20)
NOW = timezone.make_aware(datetime(2026, 3, 20, 10, 0))
REF = date(2026, 3, 19)
#: The till's window is (AFTER, coverage]: a year back from REF.
AFTER = date(2025, 3, 19)
DAY = timedelta(days=1)
D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16)

TILL_ON = Settings(use_till=True)
TILL_OFF = Settings(use_till=False)
ZERO = Decimal("0")

_SAME = object()


def article(name: str, unit=UnitChoices.LITRE, category=""):
    return make_stock_type(name=name, unit=unit, category=category)


def bought(
    at,
    what,
    day,
    qty,
    total_ht="40.00",
    *,
    product=None,
    units=None,
    colisage=1,
    spread_ht="0",
    occurred_on=None,
    invoice_date=_SAME,
):
    """One invoice of `at` dated `day` - or `invoice_date`, None for an
    undated one -, one line of `product` (a new one by default) counting
    `units` (the article quantity by default), and its PURCHASE movement of
    `qty` in the article's unit."""
    invoice = make_invoice(supplier=at, invoice_date=day if invoice_date is _SAME else (invoice_date or day))
    if invoice_date is None:
        Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
    line = make_invoice_line(
        invoice=invoice,
        product=product if product is not None else make_product(supplier=at, stock_type=what),
        quantity=Decimal(units if units is not None else qty),
        total_ht=total_ht,
        colisage=colisage,
        spread_ht=Decimal(spread_ht),
    )
    return make_movement(
        stock_type=what, quantity=qty, invoice_line=line, kind=MovementKind.PURCHASE, occurred_on=occurred_on
    )


def recipe(name: str, price, *pours):
    """A recipe pouring each (article, amount) as a fixed ingredient."""
    made = make_recipe(name=name, selling_price_ttc=price)
    for group, (stock_type, amount) in enumerate(pours):
        make_ingredient(made, stock_type=stock_type, quantity=amount, group=group)
    return made


def sold(made, quantity, day, source="caisse"):
    return RecipeSale.objects.create(recipe=made, sold_on=day, quantity=quantity, source=source)


def sold_as_itself(what, quantity, day):
    """An article sold as itself on a sale document (« bon de vente »)."""
    document = SaleDocument.objects.create(sold_on=day)
    return SaleDocumentLine.objects.create(document=document, stock_type=what, quantity=Decimal(quantity))


def sold_on_a_document(made, quantity, day):
    """A recipe sold on a sale document (« bon de vente »): the till never
    saw it."""
    document = SaleDocument.objects.create(sold_on=day)
    return SaleDocumentLine.objects.create(document=document, recipe=made, quantity=Decimal(quantity))


def covered(until):
    """The till's import recorded as complete up to `until`."""
    GatherCoverage.objects.update_or_create(
        code=auto_sales.coverage_code(sales_sources.LADDITION), defaults={"searched_until": until}
    )


def daily_totals(series) -> list[tuple[date, float]]:
    """A TillSeries back as (day, that day's consumption)."""
    return [(day, series.cum[index + 1] - series.cum[index]) for index, day in enumerate(series.days)]


# ---------------------------------------------------------------------------
# The purchases
# ---------------------------------------------------------------------------


class PurchaseScanTests(TestCase):
    def setUp(self):
        self.store = make_supplier(name="Grossiste exemple")
        self.beer = article("Bière exemple", unit=UnitChoices.UNIT)

    def days(self) -> list[date]:
        return sorted(row.day for row in purchase_rows(TODAY))

    def test_a_purchase_is_dated_by_its_invoice(self):
        bought(self.store, self.beer, D1, "24")
        [row] = purchase_rows(TODAY)
        self.assertEqual((row.article_id, row.store_id, row.day), (self.beer.pk, self.store.pk, D1))
        self.assertEqual(row.qty, Decimal("24"))

    def test_occurred_on_wins_over_the_invoice_date(self):
        bought(self.store, self.beer, D2, "24", occurred_on=D1)
        # Delivered before an invoice dated after today: the delivery counts.
        bought(self.store, self.beer, TODAY + 10 * DAY, "24", occurred_on=D3)
        # Invoiced before today, delivered after it: not yet.
        bought(self.store, self.beer, D3, "24", occurred_on=TODAY + DAY)
        self.assertEqual(self.days(), [D1, D3])

    def test_an_undated_invoice_is_left_out_whatever_its_movement_s_creation(self):
        bought(self.store, self.beer, D1, "24", invoice_date=None)
        self.assertEqual(self.days(), [])
        # Its own date, once given, is enough.
        bought(self.store, self.beer, D1, "24", invoice_date=None, occurred_on=D2)
        self.assertEqual(self.days(), [D2])

    def test_today_is_history_and_tomorrow_waits(self):
        bought(self.store, self.beer, TODAY, "24")
        bought(self.store, self.beer, TODAY + DAY, "24")
        self.assertEqual(self.days(), [TODAY])

    def test_only_the_purchase_movements_of_invoice_lines_are_read(self):
        bought(self.store, self.beer, D1, "24")
        make_movement(stock_type=self.beer, quantity="6", kind=MovementKind.PURCHASE, occurred_on=D2)
        lost_line = make_invoice_line(invoice=make_invoice(supplier=self.store, invoice_date=D3), total_ht="40.00")
        make_movement(stock_type=self.beer, quantity="-2", kind=MovementKind.LOSS, invoice_line=lost_line)
        self.assertEqual(self.days(), [D1])

    def test_a_row_carries_its_line_s_cost_units_and_pack_size(self):
        movement = bought(self.store, self.beer, D1, "24", "36.00", units="24", colisage=12, spread_ht="1.50")
        [row] = purchase_rows(TODAY)
        self.assertEqual(row.value_ht, Decimal("37.50"))
        self.assertEqual(row.product_id, movement.invoice_line.product_id)
        self.assertEqual(row.product_units, Decimal("24"))
        self.assertEqual(row.colisage, 12)

    def test_a_return_is_kept_apart_from_the_purchases_and_the_visits(self):
        bought(self.store, self.beer, D1, "24", "36.00")
        bought(self.store, self.beer, D2, "-12", "-36.00")
        self.assertEqual(sorted(row.qty for row in purchase_rows(TODAY)), [Decimal("-12"), Decimal("24")])
        prepared = prepare(TODAY, TILL_OFF, NOW)
        history = prepared.buys[self.beer.pk]
        self.assertEqual(history.days, (D1,))
        self.assertEqual(history.qty_cum[-1], Decimal("24"))
        self.assertEqual(prepared.returned[self.beer.pk].days, (D2,))
        self.assertEqual(prepared.returned[self.beer.pk].cum[-1], Decimal("12"))
        self.assertEqual(prepared.visits[self.store.pk], (D1,))


class PurchaseFilterTests(TestCase):
    """`purchase_rows`' two filters - one store, one article - narrow the
    same scan; with neither, the scan is the page's, query for query."""

    def setUp(self):
        self.shop = make_supplier(name="Grossiste exemple")
        self.grocer = make_supplier(name="Épicerie exemple")
        self.beer = article("Bière exemple", unit=UnitChoices.UNIT)
        self.syrup = article("Sirop exemple")
        for at in (self.shop, self.grocer):
            bought(at, self.beer, D1, "24")
            bought(at, self.syrup, D2, "2")
        # Never read, filtered or not: tomorrow, and undated.
        bought(self.shop, self.beer, TODAY + DAY, "24")
        bought(self.shop, self.beer, D3, "24", invoice_date=None)

    def pairs(self, **filters) -> list[tuple[int, int, date | None]]:
        return sorted((row.store_id, row.article_id, row.day) for row in purchase_rows(TODAY, **filters))

    def sql(self, **filters) -> list[str]:
        with CaptureQueriesContext(connection) as captured:
            purchase_rows(TODAY, **filters)
        return [query["sql"] for query in captured]

    def test_with_no_filter_the_scan_is_unchanged(self):
        self.assertEqual(len(self.pairs()), 4)
        self.assertEqual(self.sql(), self.sql(store_id=None, article_id=None))
        self.assertEqual(len(self.sql()), 1)

    def test_one_store(self):
        self.assertEqual(
            self.pairs(store_id=self.shop.pk),
            sorted([(self.shop.pk, self.beer.pk, D1), (self.shop.pk, self.syrup.pk, D2)]),
        )

    def test_one_article(self):
        self.assertEqual(
            self.pairs(article_id=self.syrup.pk),
            sorted([(self.shop.pk, self.syrup.pk, D2), (self.grocer.pk, self.syrup.pk, D2)]),
        )

    def test_one_article_at_one_store_in_one_query(self):
        self.assertEqual(
            self.pairs(store_id=self.grocer.pk, article_id=self.beer.pk), [(self.grocer.pk, self.beer.pk, D1)]
        )
        self.assertEqual(len(self.sql(store_id=self.grocer.pk, article_id=self.beer.pk)), 1)
        self.assertEqual(self.pairs(store_id=self.grocer.pk, article_id=0), [])


class OfferedStoresTests(TestCase):
    def test_the_suppliers_of_charges_and_the_ai_pseudo_supplier_are_not_offered(self):
        shop = make_supplier(name="Grossiste exemple")
        charges = make_supplier(name="Loyer exemple", expenses_only=True)
        reader = make_supplier(name="Analyse exemple", parser_key=LLM_PARSER_KEY)
        make_supplier(name="Fournisseur sans achat exemple")
        beer = article("Bière exemple", unit=UnitChoices.UNIT)
        bought(shop, beer, D1, "24")
        bought(charges, beer, D2, "24")
        bought(reader, beer, D3, "24")

        self.assertEqual([store.id for store in offered_stores(purchase_rows(TODAY))], [shop.pk])
        prepared = prepare(TODAY, TILL_OFF, NOW)
        self.assertEqual(set(prepared.stores), {shop.pk})
        self.assertEqual(prepared.stores[shop.pk].name, "Grossiste exemple")
        self.assertEqual([choice.store_id for choice in store_choices(prepared)], [shop.pk])
        # What was bought there is still a purchase, and a visit.
        self.assertEqual(prepared.buys[beer.pk].days, (D1, D2, D3))
        self.assertEqual(prepared.visits[charges.pk], (D2,))
        self.assertEqual(prepared.visits[reader.pk], (D3,))

    def test_a_supplier_with_returns_alone_is_not_offered(self):
        shop = make_supplier(name="Grossiste exemple")
        bought(shop, article("Fût exemple"), D1, "-30", "-60.00")
        self.assertEqual(offered_stores(purchase_rows(TODAY)), [])


class ExclusionsAndArticlesTests(TestCase):
    def test_each_kind_of_exclusion_reaches_the_pure_module(self):
        shop = make_supplier(name="Grossiste exemple")
        everywhere = article("Fût exemple")
        not_here = article("Citron exemple", unit=UnitChoices.KILOGRAM)
        ShoppingExclusion.objects.create(stock_type=everywhere)
        ShoppingExclusion.objects.create(stock_type=not_here, supplier=shop)
        ShoppingExclusion.objects.create(category="Emballage exemple")
        ShoppingExclusion.objects.create(category="")
        expected = Exclusions(
            everywhere_articles={everywhere.pk},
            categories={"Emballage exemple", ""},
            per_store={(not_here.pk, shop.pk)},
        )
        self.assertEqual(exclusions(), expected)
        self.assertEqual(prepare(TODAY, TILL_OFF, NOW).excluded, expected)

    def test_an_article_carries_its_unit_code_and_its_category(self):
        lime = article("Citron exemple", unit=UnitChoices.KILOGRAM, category="Fruits exemple")
        bare = article("Paille exemple", unit=UnitChoices.UNIT)
        infos = {info.id: info for info in article_infos()}
        self.assertEqual((infos[lime.pk].name, infos[lime.pk].unit), ("Citron exemple", UnitChoices.KILOGRAM))
        self.assertEqual(infos[lime.pk].category, "Fruits exemple")
        self.assertEqual(infos[bare.pk].category, "")

    def test_the_settings_row_becomes_the_pure_module_s_settings(self):
        self.assertEqual(settings_from(ShoppingSetting.current()), Settings(25, 6, True))
        stored = ShoppingSetting.objects.create(pk=1, threshold_percent=40, memory_months=12, use_till=False)
        self.assertEqual(settings_from(stored), Settings(threshold_percent=40, memory_months=12, use_till=False))


# ---------------------------------------------------------------------------
# The till
# ---------------------------------------------------------------------------


class TillWindowTests(TestCase):
    """The year up to the import's coverage: (AFTER, coverage]."""

    def setUp(self):
        self.shop = make_supplier(name="Grossiste exemple")
        self.syrup = article("Sirop exemple")
        bought(self.shop, self.syrup, D1, "1", "32.00")
        self.soda = recipe("Soda sirop exemple", "35", (self.syrup, "0.05"))

    def read(self):
        return read_till(purchase_rows(TODAY), NOW)

    def test_the_window_is_the_year_up_to_the_coverage(self):
        covered(REF - 5 * DAY)
        sold(self.soda, 1, AFTER)  # the window's start is excluded
        sold(self.soda, 2, AFTER + DAY)
        sold(self.soda, 4, REF - 5 * DAY)
        sold(self.soda, 8, REF - 4 * DAY)  # past the coverage: a day the import has not finished
        read = self.read()
        self.assertEqual((read.after, read.until), (AFTER, REF - 5 * DAY))
        self.assertEqual((read.data.covered_until, read.data.ref_day), (REF - 5 * DAY, REF))
        self.assertEqual(list(read.data.series[self.syrup.pk]), [(AFTER + DAY, 0.1), (REF - 5 * DAY, 0.2)])

    def test_the_till_starts_on_its_first_day_never_before_the_window(self):
        covered(REF)
        sold(self.soda, 2, D2)
        self.assertEqual(self.read().data.start, D2)
        # A till button sold earlier: the till was already open.
        button = PosProduct.objects.create(name="Bouton exemple", total_quantity=3)
        PosProductDailyQuantity.objects.create(product=button, sold_on=D1, quantity=3)
        self.assertEqual(self.read().data.start, D1)
        # Open before the window: it starts with the window, where the
        # reading starts.
        sold(self.soda, 2, AFTER - 30 * DAY)
        self.assertEqual(self.read().data.start, AFTER + DAY)

    def test_a_sale_document_before_the_import_s_first_day_does_not_move_it(self):
        """A « bon de vente » is consumption the till never saw: it counts
        among the sales, and never says when the till started."""
        covered(REF)
        sold(self.soda, 2, D2)
        sold_on_a_document(self.soda, 4, D2 - 60 * DAY)
        read = self.read()
        self.assertEqual(read.data.start, D2)
        self.assertEqual(list(read.data.series[self.syrup.pk]), [(D2 - 60 * DAY, 0.2), (D2, 0.1)])

    def test_a_hand_typed_sale_before_the_import_s_first_day_does_not_move_it(self):
        covered(REF)
        sold(self.soda, 2, D2)
        sold(self.soda, 4, D2 - 90 * DAY, source=MANUAL_SALE_SOURCE)
        read = self.read()
        self.assertEqual(read.data.start, D2)
        self.assertEqual(list(read.data.series[self.syrup.pk]), [(D2 - 90 * DAY, 0.2), (D2, 0.1)])
        # Both together, the document the earlier: still the import's day.
        sold_on_a_document(self.soda, 4, D2 - 120 * DAY)
        self.assertEqual(self.read().data.start, D2)

    def test_a_till_import_by_any_other_source_than_the_hand_starts_it(self):
        """Only a sale typed by hand is set aside: an import writes « laddition »,
        « csv » or « api:… », each the till's."""
        covered(REF)
        sold(self.soda, 2, D2, source="laddition")
        sold(self.soda, 4, D1, source="csv")
        sold(self.soda, 4, D1 - 30 * DAY, source=MANUAL_SALE_SOURCE)
        self.assertEqual(self.read().data.start, D1)

    def test_with_no_till_import_at_all_the_first_sale_starts_it(self):
        """No till import, only sales typed by hand and documents: the first
        of them starts the till, as it always did - never before W."""
        covered(REF)
        sold_on_a_document(self.soda, 4, D1)
        sold(self.soda, 2, D2, source=MANUAL_SALE_SOURCE)
        self.assertEqual(self.read().data.start, D1)
        sold(self.soda, 2, AFTER - 30 * DAY, source=MANUAL_SALE_SOURCE)
        self.assertEqual(self.read().data.start, AFTER + DAY)

    def test_with_no_coverage_recorded_the_last_recipe_sale_ends_the_window(self):
        sold(self.soda, 2, REF - 10 * DAY)
        sold(self.soda, 2, REF - 3 * DAY)
        sold(self.soda, 0, REF - DAY)  # nothing sold: no sale
        read = self.read()
        self.assertEqual(read.until, REF - 3 * DAY)
        self.assertEqual(read.data.covered_until, REF - 3 * DAY)

    def test_no_sale_in_the_window_is_no_till(self):
        covered(REF)
        self.assertIsNone(self.read())
        sold(self.soda, 2, AFTER - DAY)
        sold(self.soda, 2, TODAY)  # today is not over
        self.assertIsNone(self.read())
        self.assertIsNone(prepare(TODAY, TILL_ON, NOW).till)

    def test_a_coverage_older_than_the_window_is_no_till(self):
        covered(AFTER)
        sold(self.soda, 2, AFTER - DAY)
        self.assertIsNone(self.read())

    def test_the_engine_s_capacity_is_the_window_s_purchases_floored_at_zero(self):
        """Bought before the window, returned inside it: the window's ledger
        is negative, and a negative ceiling would let the article soak up
        sales it never covered. A fixed pour is still never clamped."""
        covered(REF)
        keg = article("Fût exemple")
        bought(self.shop, keg, AFTER - 10 * DAY, "30", "120.00")
        bought(self.shop, keg, D1, "-12", "-48.00")
        sold(recipe("Pinte exemple", "36", (keg, "0.5")), 4, D2)
        base = self.read().base
        self.assertEqual(base[keg.pk].available, ZERO)
        self.assertEqual(base[keg.pk].exact, Decimal("2"))
        # The syrup's own purchase in the window is its capacity.
        sold(self.soda, 2, D2)
        self.assertEqual(self.read().base[self.syrup.pk].available, Decimal("1"))

    def test_a_lagging_import_is_said_stale_on_the_plan(self):
        covered(REF - 10 * DAY)
        sold(self.soda, 2, D1)
        plan = plan_store(prepare(TODAY, TILL_ON, NOW), self.shop.pk, TILL_ON)
        self.assertEqual(plan.till_note.covered_until, REF - 10 * DAY)
        self.assertEqual(plan.till_note.lag_days, 10)
        self.assertTrue(plan.till_note.stale)


class TillStartOnTheLineTests(TestCase):
    """A young till import, and a sale it never saw from before it.

    « Sirop exemple » is bought 1 L every week for 40 weeks, the last two
    days ago; « Soda sirop exemple » pours 0,05 L of it; the till's import
    sold 3 sodas a day over the last 90 days and nothing before. Since the
    last litre the till poured 0,3 L, and k (bought per litre poured) is
    about 1/1,05: the till sold some 29 % of the last purchase.

    A « bon de vente », or a sale typed by hand, 250 days back predates the
    import. Counted as the till's first day, it put the months the till
    never saw into k - 1 L bought a week against 0,2 L poured - and the line
    read « la caisse a vendu 80 % du dernier achat »."""

    def setUp(self):
        covered(REF)
        self.shop = make_supplier(name="Grossiste exemple")
        self.syrup = article("Sirop exemple")
        product = make_product(supplier=self.shop, raw_name="SIROP EXEMPLE 1L", stock_type=self.syrup)
        for weeks in range(40):
            bought(self.shop, self.syrup, TODAY - (2 + 7 * weeks) * DAY, "1", "32.00", product=product)
        self.soda = recipe("Soda sirop exemple", "35", (self.syrup, "0.05"))
        for offset in range(1, 91):
            sold(self.soda, 3, TODAY - offset * DAY, source="laddition")
        self.first = TODAY - 90 * DAY

    def score(self):
        prepared = prepare(TODAY, TILL_ON, NOW)
        return prepared.till_start, score_article(prepared, self.shop.pk, self.syrup.pk, TILL_ON)

    def check_unmoved(self):
        start, before = self.score()
        self.assertEqual(start, self.first)
        self.assertEqual(before.clock, TILL_CLOCK)
        self.assertAlmostEqual(before.till_share, 0.3 / 1.05, places=9)
        return before

    def assert_same_line(self, before):
        start, after = self.score()
        self.assertEqual(start, self.first)
        self.assertEqual(after.clock, TILL_CLOCK)
        self.assertAlmostEqual(after.till_share, before.till_share, places=9)
        self.assertAlmostEqual(after.need, before.need, places=9)
        self.assertAlmostEqual(after.rate, before.rate, places=9)

    def test_a_sale_document_before_the_import_changes_nothing_on_the_line(self):
        before = self.check_unmoved()
        sold_on_a_document(self.soda, 4, TODAY - 250 * DAY)
        self.assert_same_line(before)

    def test_a_hand_typed_sale_before_the_import_changes_nothing_on_the_line(self):
        before = self.check_unmoved()
        sold(self.soda, 4, TODAY - 250 * DAY, source=MANUAL_SALE_SOURCE)
        self.assert_same_line(before)


class DailySpreadTests(TestCase):
    """One worked example with an « OU »: « Punch exemple » pours 4 cl of
    the amber OR the white rum, and 2 cl of lime; « Long drink exemple » pours
    2 cl of the amber rum; 50 cl of the white rum are sold as themselves.

    Over the window the amber rum was bought 1 L (3 L more a year before,
    out of the window's capacity), the white rum 5 L, the lime 2 kg; both
    rums lose 10 %. The engine charges the fixed pours first (amber 0,20,
    white 0,50, lime 1,20), then serves the 60 punches priciest first: the
    amber rum takes 18 (its 0,90 allowance, less its 0,20, is 17,5 servings,
    rounded up), 0,72 L; the white rum the other 42, 1,68 L.

    The punches sold 15, 15 and 30 on three days, so each day takes a
    quarter, a quarter and a half of each rum's share. « Rhum épicé
    exemple », the same « OU », sold 2 and was refunded 2: nothing over the
    window, so the engine allocates it nothing, and no day's share moves."""

    def setUp(self):
        covered(REF)
        shop = make_supplier(name="Grossiste exemple")
        self.amber = article("Rhum ambré exemple")
        self.white = article("Rhum blanc exemple")
        self.lime = article("Agrume exemple", unit=UnitChoices.KILOGRAM)
        bought(shop, self.amber, date(2026, 2, 2), "1", "40.00")
        bought(shop, self.amber, date(2025, 1, 10), "3", "120.00")
        bought(shop, self.white, date(2026, 2, 2), "5", "50.00")
        bought(shop, self.lime, date(2026, 2, 2), "2", "36.00")
        punch = make_recipe(name="Punch exemple", selling_price_ttc="35")
        make_ingredient(punch, stock_type=self.amber, quantity="0.04", group=0)
        make_ingredient(punch, stock_type=self.white, quantity="0.04", group=0)
        make_ingredient(punch, stock_type=self.lime, quantity="0.02", group=1)
        planter = recipe("Long drink exemple", "38", (self.amber, "0.02"))
        refunded = make_recipe(name="Rhum épicé exemple", selling_price_ttc="36")
        make_ingredient(refunded, stock_type=self.amber, quantity="0.04", group=0)
        make_ingredient(refunded, stock_type=self.white, quantity="0.04", group=0)
        sold(punch, 15, D1)
        sold(punch, 15, D2)
        sold(punch, 30, D3)
        sold(planter, 10, D2)
        sold(refunded, 2, D1)
        sold(refunded, -2, D3)
        sold_as_itself(self.white, "0.5", D3)
        self.read = read_till(purchase_rows(TODAY), NOW)

    def test_the_engine_serves_the_choice_priciest_first_within_the_window_s_purchases(self):
        base = self.read.base
        self.assertEqual(base[self.amber.pk].available, Decimal("1"))
        self.assertEqual((base[self.amber.pk].exact, base[self.amber.pk].shared), (Decimal("0.2"), Decimal("0.72")))
        self.assertEqual((base[self.white.pk].exact, base[self.white.pk].shared), (Decimal("0.5"), Decimal("1.68")))
        self.assertEqual((base[self.lime.pk].exact, base[self.lime.pk].shared), (Decimal("1.2"), ZERO))

    def test_each_day_takes_its_fixed_pours_and_its_share_of_the_choice(self):
        daily = self.read.daily
        expected = {
            self.amber.pk: {D1: ("0", "0.18"), D2: ("0.2", "0.18"), D3: ("0", "0.36")},
            self.white.pk: {D1: ("0", "0.42"), D2: ("0", "0.42"), D3: ("0.5", "0.84")},
            self.lime.pk: {D1: ("0.3", "0"), D2: ("0.3", "0"), D3: ("0.6", "0")},
        }
        for article_id, days in expected.items():
            self.assertEqual(
                daily[article_id],
                {day: DailyUse(Decimal(exact), Decimal(shared)) for day, (exact, shared) in days.items()},
            )

    def test_the_days_add_up_to_attribute_sales_over_the_window(self):
        """The engine run on its own over the same window, with the capacity
        and costs worked out by hand: the window's purchases, and the
        average cost of every purchase."""
        with variation_scope():
            sales = read_sales(self.read.after, self.read.until)
            for made in sales.recipes:
                sales.terms_of(made)
        engine = attribute_sales(
            sales,
            {self.amber.pk: Decimal("1"), self.white.pk: Decimal("5"), self.lime.pk: Decimal("2")},
            {self.amber.pk: Decimal("40"), self.white.pk: Decimal("10"), self.lime.pk: Decimal("18")},
        )
        for article_id in (self.amber.pk, self.white.pk, self.lime.pk):
            days = self.read.daily[article_id].values()
            self.assertEqual(sum((use.exact for use in days), ZERO), engine[article_id].exact)
            self.assertEqual(sum((use.shared for use in days), ZERO), engine[article_id].shared)

    def test_the_pure_module_gets_each_day_s_total_as_a_float(self):
        till = prepare(TODAY, TILL_ON, NOW).till
        for article_id, expected in (
            (self.amber.pk, [(D1, 0.18), (D2, 0.38), (D3, 0.36)]),
            (self.lime.pk, [(D1, 0.3), (D2, 0.3), (D3, 0.6)]),
        ):
            got = daily_totals(till[article_id])
            self.assertEqual([day for day, _quantity in got], [day for day, _quantity in expected])
            for (_day, quantity), (_same, wanted) in zip(got, expected, strict=True):
                self.assertAlmostEqual(quantity, wanted, places=12)


class TillOffTests(TestCase):
    def test_with_the_till_off_no_sales_table_is_read_and_no_till_is_made(self):
        covered(REF)
        shop = make_supplier(name="Grossiste exemple")
        syrup = article("Sirop exemple")
        bought(shop, syrup, D1, "1", "32.00")
        sold(recipe("Soda sirop exemple", "35", (syrup, "0.05")), 2, D2)
        with CaptureQueriesContext(connection) as captured:
            prepared = prepare(TODAY, TILL_OFF, NOW)
        self.assertIsNone(prepared.till)
        for query in captured:
            for table in ("recipes_", "invoices_gathercoverage", "notifications_"):
                self.assertNotIn(table, query["sql"])
        # The same history with the till on reads it.
        self.assertIsNotNone(prepare(TODAY, TILL_ON, NOW).till)


class QueryCountTests(TestCase):
    """Three times the purchases and the sales cost the same queries."""

    #: What `prepare` reads with the till off: the scan, the articles, the
    #: suppliers, the products' names, the exclusions.
    TILL_OFF_QUERIES = 5
    #: And with it on: the night and the coverage, the window's recipe
    #: sales and sale-document lines, the till's first day (two), and the
    #: engine's reading of the recipes inside one variation_scope.
    TILL_ON_QUERIES = 20

    def setUp(self):
        covered(REF)
        self.shop = make_supplier(name="Grossiste exemple")
        self.syrup = article("Sirop exemple")
        self.amber = article("Rhum ambré exemple")
        self.white = article("Rhum blanc exemple")
        self.punch = make_recipe(name="Punch exemple", selling_price_ttc="35")
        make_ingredient(self.punch, stock_type=self.amber, quantity="0.04", group=0)
        make_ingredient(self.punch, stock_type=self.white, quantity="0.04", group=0)
        make_ingredient(self.punch, stock_type=self.syrup, quantity="0.01", group=1)
        self.days = (REF - offset * DAY for offset in range(1, 60))
        self.grow(6)

    def grow(self, count: int) -> None:
        for _ in range(count):
            day = next(self.days)
            for what in (self.syrup, self.amber, self.white):
                bought(self.shop, what, day, "1", "40.00")
            sold(self.punch, 3, day)
            sold_as_itself(self.white, "0.25", day)

    def queries(self, settings) -> int:
        with CaptureQueriesContext(connection) as captured:
            prepare(TODAY, settings, NOW)
        return len(captured)

    def test_more_history_costs_no_more_queries_with_the_till_off(self):
        few = self.queries(TILL_OFF)
        self.grow(12)
        self.assertEqual(self.queries(TILL_OFF), few)

    def test_more_history_costs_no_more_queries_with_the_till_on(self):
        few = self.queries(TILL_ON)
        self.grow(12)
        self.assertEqual(self.queries(TILL_ON), few)

    def test_the_queries_are_pinned(self):
        with self.assertNumQueries(self.TILL_OFF_QUERIES):
            prepare(TODAY, TILL_OFF, NOW)
        with self.assertNumQueries(self.TILL_ON_QUERIES):
            prepare(TODAY, TILL_ON, NOW)


# ---------------------------------------------------------------------------
# From the database to the list
# ---------------------------------------------------------------------------


class PlanFromTheDatabaseTests(TestCase):
    """Ten weekly visits to one store, the last a week ago, and the till's
    import up to yesterday:

    * « Bière exemple », 24 cans at every visit: a habit, one purchase
      lasting a week - on the list, its usual product and its pack size;
    * « Sirop exemple », 1 L at every visit, which a recipe pours 0,1 L a day
      of: the till says the last litre is gone - on the list, by the till;
    * « Chips exemple », bought once four weeks ago: not yet a habit;
    * « Gobelet exemple », in a category left out, and « Citron exemple »,
      left out at this store (« Pas ici »): never candidates."""

    def setUp(self):
        covered(REF)
        self.shop = make_supplier(name="Grossiste exemple")
        beer = article("Bière exemple", unit=UnitChoices.UNIT)
        syrup = article("Sirop exemple")
        cups = article("Gobelet exemple", unit=UnitChoices.UNIT, category="Emballage exemple")
        lime = article("Citron exemple", unit=UnitChoices.KILOGRAM)
        chips = article("Chips exemple", unit=UnitChoices.UNIT)
        products = {
            what: make_product(supplier=self.shop, raw_name=name, stock_type=what)
            for what, name in (
                (beer, "BIERE EXEMPLE 33CL X24"),
                (syrup, "SIROP EXEMPLE 1L"),
                (cups, "GOBELET EXEMPLE X100"),
                (lime, "CITRON EXEMPLE KG"),
            )
        }
        for weeks in range(10, 0, -1):
            day = TODAY - 7 * weeks * DAY
            bought(self.shop, beer, day, "24", "38.40", product=products[beer], colisage=24)
            bought(self.shop, syrup, day, "1", "32.00", product=products[syrup])
            bought(self.shop, cups, day, "100", "35.00", product=products[cups])
            bought(self.shop, lime, day, "1", "31.00", product=products[lime])
        bought(self.shop, chips, TODAY - 28 * DAY, "10", "33.00")
        ShoppingExclusion.objects.create(category="Emballage exemple")
        ShoppingExclusion.objects.create(stock_type=lime, supplier=self.shop)
        soda = recipe("Soda sirop exemple", "35", (syrup, "0.05"))
        for offset in range(1, 91):
            sold(soda, 2, TODAY - offset * DAY)
        self.plan = plan_store(prepare(TODAY, TILL_ON, NOW), self.shop.pk, TILL_ON)

    def test_the_history_gives_the_expected_list(self):
        plan = self.plan
        self.assertEqual(plan.store_name, "Grossiste exemple")
        self.assertEqual((plan.visits, plan.last_visit, plan.usual_gap), (10, TODAY - 7 * DAY, 7.0))
        self.assertEqual(plan.candidates, 3)
        self.assertEqual(sorted(line.name for line in plan.to_buy), ["Bière exemple", "Sirop exemple"])
        self.assertEqual(plan.maybe, ())
        self.assertEqual([line.name for line in plan.new], ["Chips exemple"])
        self.assertEqual((plan.till_note.covered_until, plan.till_note.stale), (REF, False))

    def test_the_beer_is_its_usual_product_by_the_calendar(self):
        [line] = [line for line in self.plan.to_buy if line.name == "Bière exemple"]
        self.assertEqual(line.clock, CALENDAR_CLOCK)
        self.assertEqual(line.qty, Decimal("24"))
        self.assertEqual((line.product_name, line.product_units), ("BIERE EXEMPLE 33CL X24", Decimal("24")))
        self.assertEqual(line.packs, (1, Decimal("24")))

    def test_the_syrup_is_listed_by_the_till(self):
        """9 L bought over the 63 days the till poured 6,3 L: k is 9/6,3.
        Since the last litre, a week ago, the till poured 0,7 L, and pours
        0,1 L a day: need = k x (0,7 + 0,1 x 7) / 1 L = 2."""
        [line] = [line for line in self.plan.to_buy if line.name == "Sirop exemple"]
        self.assertEqual(line.clock, TILL_CLOCK)
        self.assertAlmostEqual(line.need, 2.0, places=9)
        self.assertIn(" ; la caisse a ", line.sentence)
        self.assertEqual(line.product_name, "SIROP EXEMPLE 1L")


# ---------------------------------------------------------------------------
# One article's usual purchase at one store
# ---------------------------------------------------------------------------


class UsualPurchaseAtTests(TestCase):
    """`usual_purchase_at(today, store, article)`: the usual purchase the
    forecast's line shows (`shopping.usual_purchase` over the whole
    `prepare()`), read for one article at one store - two queries at most,
    whatever the history. make_shopping_history's every article at each of
    its stores (invented data)."""

    @classmethod
    def setUpTestData(cls):
        cls.made = make_shopping_history()

    def articles(self):
        made = self.made
        return (
            made.beer,
            made.syrup,
            made.olives,
            made.crisps,
            made.rum,
            made.coffee,
            made.lemon,
            made.keg,
            made.strawberries,
        )

    def test_it_equals_the_whole_page_s_reading_for_every_article_at_every_store(self):
        today = timezone.localdate()
        whole = prepare(today, TILL_OFF, timezone.now())
        found = 0
        for store in (self.made.wholesaler, self.made.grocer, self.made.market):
            for what in self.articles():
                with self.subTest(store=store.name, article=what.name):
                    expected = shopping.usual_purchase(whole, store.pk, what.pk)
                    with CaptureQueriesContext(connection) as captured:
                        got = shopping_data.usual_purchase_at(today, store.pk, what)
                    self.assertEqual(got, expected)
                    self.assertLessEqual(len(captured), 2)
                    found += got is not None
        # The beer, syrup, olives, crisps, rum, coffee and keg at the
        # wholesaler's, the coffee and lemons at the grocer's, the
        # strawberries at the market.
        self.assertEqual(found, 10)

    def test_the_beer_in_its_store_s_product(self):
        usual = shopping_data.usual_purchase_at(timezone.localdate(), self.made.wholesaler.pk, self.made.beer)
        self.assertEqual(usual.qty, Decimal("24"))
        self.assertEqual((usual.product_name, usual.product_units), (SHOPPING_BEER_PRODUCT, Decimal("24")))
        self.assertEqual(usual.packs, (1, Decimal("24")))

    def test_never_bought_there_is_one_query_and_none(self):
        with self.assertNumQueries(1):
            self.assertIsNone(
                shopping_data.usual_purchase_at(timezone.localdate(), self.made.grocer.pk, self.made.beer)
            )

    def test_returns_alone_are_no_usual_purchase(self):
        # The keg, given back at the market and never bought there.
        bought(self.made.market, self.made.keg, timezone.localdate() - 3 * DAY, "-1", "-90.00")
        self.assertIsNone(shopping_data.usual_purchase_at(timezone.localdate(), self.made.market.pk, self.made.keg))

    def test_a_purchase_after_today_waits(self):
        tomorrow = timezone.localdate() + DAY
        bought(self.made.market, self.made.olives, tomorrow, "3", "35.00")
        self.assertIsNone(shopping_data.usual_purchase_at(timezone.localdate(), self.made.market.pk, self.made.olives))
        self.assertEqual(shopping_data.usual_purchase_at(tomorrow, self.made.market.pk, self.made.olives).qty, 3)
