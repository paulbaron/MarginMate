"""« Produits & charges » entre deux dates.

The owner, 20/09: on this page I want to see only the products bought
between two dates. So the page takes a « Du … au … » (common.date_range,
`?du=&au=`) and answers it literally:

* what a window shows is what was BOUGHT in it - the per-article figures,
  the category totals and the headline count movements whose
  `StockMovement.effective_date` falls between the two dates, both ends
  included, and an article nothing was bought of is not a row of zeroes,
  it is not on the page at all;
* « Vendu » beside « Acheté » is the sales over the SAME window, or the
  column lies. `recipes.sales.sales_between` EXCLUDES its start day (a sale
  on the day of a stock count belongs to the count), so a window starting
  « du 1er » has to hand it the 31st or it silently loses a day of sales;
* the charges follow the window instead of their default twelve months;
* an `?inventaire=` chosen wins: it is two physical counts with its own
  arithmetic, which two free dates cannot produce;
* `?du=n-importe-quoi` is the whole page, never a 500 - this arrives from a
  query string;
* and the window travels on the list's own htmx reload, or classifying a
  product throws the reader back to all time without a word.

Data invented.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import MovementKind, StockMovement, StockType, UnitChoices
from invoices.models import Invoice
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

D = Decimal

#: « du 01/02/2026 au 28/02/2026 », the window every test below asks for.
FEBRUARY = {"du": "2026-02-01", "au": "2026-02-28"}


class PurchaseWindowTestCase(TestCase):
    """One supplier, articles bought on dates chosen to sit on either side
    of February and on its very edges."""

    def setUp(self):
        self.url = reverse("inventory:stock_list")
        self.supplier = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple")
        self._products = {}

    def product_for(self, stock_type):
        """One product per article, bought again and again - a supplier
        cannot have the same raw name twice, and that is also what really
        happens: one reference, several deliveries."""
        if stock_type.pk not in self._products:
            self._products[stock_type.pk] = make_product(
                supplier=self.supplier,
                raw_name=f"{stock_type.name.upper()} EXEMPLE",
                stock_type=stock_type,
                unit=stock_type.unit,
            )
        return self._products[stock_type.pk]

    def buy(self, stock_type, on: date, quantity="1", total_ht="20", vat_rate="0.20"):
        """A delivery: an invoice dated `on`, its line, and the movement it
        books - the shape every real purchase has on this page."""
        product = self.product_for(stock_type)
        invoice = make_invoice(supplier=self.supplier, invoice_date=on)
        line = make_invoice_line(
            invoice=invoice, product=product, quantity=int(quantity),
            total_ht=total_ht, vat_rate=D(vat_rate),
        )
        return make_movement(
            stock_type=stock_type, quantity=quantity,
            unit_cost_ht=D(total_ht) / D(quantity), invoice_line=line,
        )

    def page(self, **parameters):
        return self.client.get(self.url, parameters)

    def rows(self, response):
        return {
            row["stock_type"].name: row
            for category in response.context["categories"]
            for row in category["rows"]
        }


class WhatTheWindowCountsTests(PurchaseWindowTestCase):
    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        # The same article, bought again in January: outside the window, so
        # neither its litres nor its euros may turn up in February.
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")

    def test_the_figures_count_only_what_the_window_holds(self):
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        self.assertEqual(row["quantity"], D("12"))
        self.assertEqual(row["value_ht"], D("240"))
        self.assertEqual(row["value_ttc"], D("288"))

    def test_the_category_and_page_totals_follow(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(response.context["categories"][0]["total_value_ht"], D("240"))
        self.assertEqual(response.context["categories"][0]["total_value_ttc"], D("288"))
        self.assertEqual(response.context["total_value_ht"], D("240"))
        self.assertEqual(response.context["total_value_ttc"], D("288"))

    def test_without_the_window_everything_is_counted(self):
        """The window is the only difference: unasked, the page is what it
        always was."""
        row = self.rows(self.page())["Vodka"]
        self.assertEqual((row["quantity"], row["value_ht"]), (D("18"), D("360")))
        self.assertEqual(self.page().context["total_value_ht"], D("360"))

    def test_both_ends_are_included(self):
        """« au 28 » means the 28th. A delivery on either edge is in."""
        opening_day = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        closing_day = make_stock_type(name="Gin", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(opening_day, date(2026, 2, 1), quantity="2", total_ht="40")
        self.buy(closing_day, date(2026, 2, 28), quantity="3", total_ht="60")
        # Bought outside as well, so the edge day is the only thing these
        # two figures can be: all time they are 7 and 10.
        self.buy(opening_day, date(2026, 1, 15), quantity="5", total_ht="100")
        self.buy(closing_day, date(2026, 3, 15), quantity="7", total_ht="140")
        rows = self.rows(self.page(**FEBRUARY))
        self.assertEqual(rows["Rhum"]["quantity"], D("2"))
        self.assertEqual(rows["Gin"]["quantity"], D("3"))

    def test_the_days_just_outside_are_out(self):
        before = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        after = make_stock_type(name="Gin", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(before, date(2026, 1, 31), quantity="2", total_ht="40")
        self.buy(after, date(2026, 3, 1), quantity="3", total_ht="60")
        rows = self.rows(self.page(**FEBRUARY))
        self.assertNotIn("Rhum", rows)
        self.assertNotIn("Gin", rows)

    def test_one_end_alone_is_a_window(self):
        since = self.rows(self.page(du="2026-02-01"))["Vodka"]
        self.assertEqual(since["quantity"], D("12"))
        until = self.rows(self.page(au="2026-01-31"))["Vodka"]
        self.assertEqual(until["quantity"], D("6"))


class APriceIsNotAPropertyOfTheWindowTests(PurchaseWindowTestCase):
    """`unit_costs` stays all time, window or not: a price is what an
    article costs, not something a pair of dates decides. A window holding
    one cheap delivery would otherwise value every bottle at that delivery's
    price - and those prices are what decides which alternative a shared
    sale is charged to (variance.order_options fills the priciest first).

    So: two spirits offered as alternatives, whose all-time order is the
    reverse of their order inside the window.
    """

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.gin = make_stock_type(name="Gin", unit=UnitChoices.LITRE, category="Spiritueux")
        # All time: Vodka 1010/20 = 50,50 € the litre, Gin 210/20 = 10,50.
        # Inside February: Vodka 1 €, Gin 20 € - the other way round.
        self.buy(self.vodka, date(2026, 1, 10), quantity="10", total_ht="1000")
        self.buy(self.vodka, date(2026, 2, 10), quantity="10", total_ht="10")
        self.buy(self.gin, date(2026, 1, 10), quantity="10", total_ht="10")
        self.buy(self.gin, date(2026, 2, 10), quantity="10", total_ht="200")
        recipe = make_recipe(name="Vodka ou gin tonic")
        make_ingredient(recipe, stock_type=self.vodka, quantity="1", group=0)
        make_ingredient(recipe, stock_type=self.gin, quantity="1", group=0)
        record_sales([("Vodka ou gin tonic", date(2026, 2, 15), 5)])

    def test_the_shared_sale_follows_the_all_time_price(self):
        rows = self.rows(self.page(**FEBRUARY))
        self.assertEqual(rows["Vodka"]["sold"].headline, D("5"))
        self.assertEqual(rows["Gin"]["sold"].headline, D("0"))

    def test_the_window_still_decides_what_was_bought(self):
        rows = self.rows(self.page(**FEBRUARY))
        self.assertEqual(rows["Vodka"]["value_ht"], D("10"))
        self.assertEqual(rows["Gin"]["value_ht"], D("200"))


class WhichDateAMovementCountsOnTests(PurchaseWindowTestCase):
    """`StockMovement.effective_date` is a property with three fallbacks,
    and the page's single scan reproduces them in the same order - reading
    them off model instances would be a query per movement."""

    def test_a_movement_dated_by_its_own_occurred_on(self):
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        make_movement(stock_type=vodka, quantity="4", unit_cost_ht="20", occurred_on=date(2026, 2, 14))
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("4"))
        self.assertNotIn("Vodka", self.rows(self.page(du="2026-03-01")))

    def test_occurred_on_beats_the_invoice_date(self):
        """A delivery invoiced in January and received in February counts in
        February - which is the whole reason `occurred_on` exists."""
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        movement = self.buy(vodka, date(2026, 1, 10), quantity="5", total_ht="100")
        movement.occurred_on = date(2026, 2, 14)
        movement.save(update_fields=["occurred_on"])
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("5"))

    def test_a_movement_dated_by_its_invoice(self):
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(vodka, date(2026, 2, 10), quantity="5", total_ht="100")
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("5"))
        self.assertNotIn("Vodka", self.rows(self.page(du="2026-03-01")))

    def test_a_movement_dated_by_neither_falls_back_on_when_it_was_typed(self):
        """No `occurred_on`, no invoice: all that is left is `created_at`,
        and a movement typed in on the 14th belongs to February."""
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        movement = make_movement(stock_type=vodka, quantity="7", unit_cost_ht="20")
        # auto_now_add cannot be assigned, and a plain UPDATE is the one way
        # to give a row a creation date a test chooses.
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime(2026, 2, 14, 12, 0))
        )
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("7"))
        self.assertNotIn("Vodka", self.rows(self.page(du="2026-03-01")))

    def test_an_invoice_with_no_date_at_all_is_in_no_window(self):
        """« Sans date » is where an undated document is looked at, not
        inside a pair of dates - but `created_at` still dates the movement,
        so the row lands in the window it was typed in and nowhere else."""
        vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        invoice = make_invoice(supplier=self.supplier)
        Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
        line = make_invoice_line(
            invoice=invoice, product=self.product_for(vodka), quantity=3, total_ht="60"
        )
        movement = make_movement(stock_type=vodka, quantity="3", unit_cost_ht="20", invoice_line=line)
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 20, 12, 0))
        )
        self.assertNotIn("Vodka", self.rows(self.page(**FEBRUARY)))
        self.assertEqual(self.rows(self.page(du="2026-05-01", au="2026-05-31"))["Vodka"]["quantity"], D("3"))


class OnlyWhatWasBoughtIsListedTests(PurchaseWindowTestCase):
    """« Only see the products bought between 2 dates » - an article with
    nothing bought in the window is off the page, and an empty category
    goes with it."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.tonic = make_stock_type(name="Tonic", unit=UnitChoices.LITRE, category="Softs")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.tonic, date(2026, 1, 10), quantity="24", total_ht="48")

    def test_an_article_bought_only_outside_the_window_is_absent(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(list(self.rows(response)), ["Vodka"])
        self.assertNotContains(response, ">Tonic<")

    def test_its_category_goes_with_it(self):
        response = self.page(**FEBRUARY)
        self.assertEqual([category["name"] for category in response.context["categories"]], ["Spiritueux"])

    def test_an_article_never_bought_at_all_is_absent_too(self):
        make_stock_type(name="Absinthe", unit=UnitChoices.LITRE, category="Spiritueux")
        self.assertNotIn("Absinthe", self.rows(self.page(**FEBRUARY)))
        # Unasked, it is listed as it always was: nothing bought is a row of
        # zeroes on the all-time page, which is where one goes to find it.
        self.assertIn("Absinthe", self.rows(self.page()))

    def test_the_page_says_how_many_articles_the_window_holds(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(response.context["stock_type_count"], 1)
        self.assertContains(response, "Du 01/02/2026 au 28/02/2026 :")
        self.assertContains(response, "1 article")

    def test_two_empty_dates_read_as_empty_dates_not_as_a_broken_page(self):
        response = self.page(du="2026-06-01", au="2026-06-30")
        self.assertEqual(response.context["categories"], [])
        self.assertEqual(response.context["stock_type_count"], 0)
        self.assertContains(response, "Aucun produit acheté du 01/06/2026 au 30/06/2026")
        self.assertNotContains(response, "Aucun article pour le moment")

    def test_the_window_is_named_in_the_headline(self):
        response = self.page(**FEBRUARY)
        self.assertContains(response, "produits, du 01/02/2026 au 28/02/2026 — hors charges")
        self.assertNotContains(response, "produits, depuis le début")

    def test_one_end_alone_is_named_in_words_a_person_wrote(self):
        self.assertContains(self.page(du="2026-02-01"), "depuis le 01/02/2026")
        self.assertContains(self.page(au="2026-02-28"), "jusqu&#x27;au 28/02/2026")


class SoldOverTheSameWindowTests(PurchaseWindowTestCase):
    """« Vendu » beside « Acheté » has to be the same window, or the column
    lies - and `sales_between` excludes its start day, which is exactly how
    a window would silently lose one."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 1), quantity="40", total_ht="800")
        recipe = make_recipe(name="Vodka sec")
        make_ingredient(recipe, stock_type=self.vodka, quantity="1")
        record_sales(
            [
                ("Vodka sec", date(2026, 1, 31), 7),   # the day before « du » : out
                ("Vodka sec", date(2026, 2, 1), 3),    # « du » itself : in
                ("Vodka sec", date(2026, 2, 28), 2),   # « au » itself : in
                ("Vodka sec", date(2026, 3, 1), 4),    # the day after : out
            ]
        )

    def test_the_sales_of_the_window_and_of_no_other_day(self):
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        self.assertEqual(row["sold"].headline, D("5"))

    def test_the_first_day_of_the_window_counts(self):
        """The half-open trap: handing « du » straight to sales_between
        would drop the 1st, and this row would read 2 instead of 5."""
        one_day = self.rows(self.page(du="2026-02-01", au="2026-02-01"))["Vodka"]
        self.assertEqual(one_day["sold"].headline, D("3"))

    def test_the_ceiling_is_windowed_too(self):
        """`available` is what the window bought, less the losses written
        down in it - all-time purchases against one month of sales would
        say nothing is ever missing."""
        self.buy(self.vodka, date(2026, 1, 5), quantity="100", total_ht="2000")
        make_movement(
            stock_type=self.vodka, kind=MovementKind.LOSS, quantity="-10",
            unit_cost_ht="20", occurred_on=date(2026, 2, 20),
        )
        make_movement(
            stock_type=self.vodka, kind=MovementKind.LOSS, quantity="-7",
            unit_cost_ht="20", occurred_on=date(2026, 1, 20),
        )
        # 40 bought in February less the 10 lost in it - not the 123 the
        # whole ledger holds.
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        self.assertEqual(row["sold"].available, D("30"))
        self.assertEqual(self.rows(self.page())["Vodka"]["sold"].available, D("123"))

    def test_more_sold_than_the_window_bought_is_flagged(self):
        small = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(small, date(2026, 2, 3), quantity="1", total_ht="20")
        # A cellar bought in January: all time this bottle is not short of
        # anything, and only the window makes the six litres poured in
        # February more than February can account for.
        self.buy(small, date(2026, 1, 3), quantity="50", total_ht="1000")
        recipe = make_recipe(name="Rhum sec")
        make_ingredient(recipe, stock_type=small, quantity="1")
        record_sales([("Rhum sec", date(2026, 2, 10), 6)])
        response = self.page(**FEBRUARY)
        row = self.rows(response)["Rhum"]
        self.assertTrue(row["flag_over"])
        # The count beside the list is counted over the list's own window.
        self.assertEqual(response.context["over_stock_count"], 1)
        self.assertFalse(self.rows(self.page())["Rhum"]["flag_over"])


class ChargesFollowTheWindowTests(PurchaseWindowTestCase):
    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.bailleur = make_supplier(code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True)
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        for day, amount in ((date(2026, 2, 5), "500"), (date(2026, 1, 5), "500"), (date(2026, 3, 5), "500")):
            bill = make_invoice(supplier=self.bailleur, invoice_date=day)
            make_invoice_line(invoice=bill, product=self.loyer, total_ht=amount, vat_rate=D("0.20"))

    def test_only_the_charges_of_the_window_are_totalled(self):
        response = self.page(**FEBRUARY)
        (row,) = response.context["charge_suppliers"]
        self.assertEqual(row["documents"], 1)
        self.assertEqual(row["total_ttc"], D("600.00"))
        self.assertEqual(response.context["charge_total_ttc"], D("600.00"))

    def test_the_supplier_stays_listed_with_its_last_document(self):
        """A supplier that billed nothing over the window keeps its row and
        the date of its last document - a bill arriving twice a year would
        otherwise drop off the page between two of them."""
        (row,) = self.page(du="2026-06-01", au="2026-06-30").context["charge_suppliers"]
        self.assertEqual(row["documents"], 0)
        self.assertEqual(row["documents_all"], 3)
        self.assertEqual(row["last"], date(2026, 3, 5))

    def test_both_edges_of_the_window_are_charged(self):
        """The charges are narrowed in SQL rather than by `DateRange.limit`,
        so their own edges have to be proved: the 1st and the 28th are in, a
        day either side is out. The bills above sit a month away from the
        window, where an off-by-one changes nothing."""
        for day, amount in (
            (date(2026, 1, 31), "10"), (date(2026, 2, 1), "20"),
            (date(2026, 2, 28), "30"), (date(2026, 3, 1), "40"),
        ):
            edge = make_invoice(supplier=self.bailleur, invoice_date=day)
            make_invoice_line(invoice=edge, product=self.loyer, total_ht=amount, vat_rate=D("0"))
        (row,) = self.page(**FEBRUARY).context["charge_suppliers"]
        # The February rent of the setUp (600 TTC) plus the two edge days.
        self.assertEqual(row["documents"], 3)
        self.assertEqual(row["total_ttc"], D("650.00"))

    def test_the_wording_says_the_window_rather_than_twelve_months(self):
        response = self.page(**FEBRUARY)
        self.assertContains(response, "Documents et totaux du 01/02/2026 au 28/02/2026")
        self.assertNotContains(response, "Documents et totaux sur les douze derniers mois")

    def test_without_a_window_the_charges_are_the_last_twelve_months(self):
        """Unasked, the fold is what it always was - an all-time total of a
        monthly subscription says little. Dated from today rather than from
        a fixed month, since that default moves with the calendar."""
        today = timezone.localdate()
        operator = make_supplier(
            code="OPERATEUR_X", name="Opérateur Exemple", parser_key="", expenses_only=True
        )
        abonnement = make_product(supplier=operator, raw_name="ABONNEMENT", is_expense=True)
        for day, amount in ((today - timedelta(days=30), "100"), (today - timedelta(days=400), "999")):
            bill = make_invoice(supplier=operator, invoice_date=day)
            make_invoice_line(invoice=bill, product=abonnement, total_ht=amount, vat_rate=D("0.20"))
        response = self.page()
        (row,) = [row for row in response.context["charge_suppliers"] if row["supplier"] == operator]
        self.assertEqual(row["total_ttc"], D("120.00"))
        self.assertEqual(row["documents_all"], 2)
        self.assertContains(response, "sur les douze derniers mois")


class AWindowWithNoPurchasesStillHasFiguresTests(PurchaseWindowTestCase):
    """Two dates over which nothing was bought but the rent was paid.

    The headline row was drawn « si des catégories », which until the window
    shipped meant « sur une base neuve » - and a window with no purchase in
    it empties the categories, so the whole row went: « Total acheté »,
    « Articles » and « Charges (TTC) » all disappeared while the fold below
    went on showing the 600 € those dates cost. A figure that vanishes reads
    as a broken page, which is the one thing these dates must never do.
    """

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        bill = make_invoice(supplier=self.bailleur, invoice_date=date(2026, 2, 5))
        make_invoice_line(invoice=bill, product=self.loyer, total_ht="500", vat_rate=D("0.20"))

    def test_the_charge_paid_over_those_dates_is_still_in_the_headline(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(response.context["categories"], [])
        self.assertEqual(response.context["charge_total_ttc"], D("600.00"))
        self.assertContains(response, "Charges (TTC)")
        self.assertContains(response, "600.00 €")

    def test_and_the_purchases_are_said_to_be_nil_rather_than_left_out(self):
        response = self.page(**FEBRUARY)
        self.assertContains(response, "Total acheté (HT)")
        self.assertContains(response, "produits, du 01/02/2026 au 28/02/2026 — hors charges")
        self.assertContains(response, "Aucun produit acheté du 01/02/2026 au 28/02/2026")

    def test_a_base_with_nothing_on_it_still_draws_no_figures(self):
        """The guard the above widens is also what keeps a new install from
        opening on a row of zeroes - so it must still do that. Nothing asked
        for and no article at all: the page invites, it does not total."""
        StockMovement.objects.all().delete()
        StockType.objects.all().delete()
        response = self.page()
        self.assertNotContains(response, "Total acheté (HT)")
        self.assertContains(response, "Aucun article pour le moment")


class WhatARowOpensIsTheWindowToTests(PurchaseWindowTestCase):
    """A row's figures are the window's, and so is what it OPENS - « filtre
    aussi les articles avec les dates quand on déroule » (owner, 20/09).
    The curve alone stays whole: narrowed to a month it is two points and
    « pas assez d'historique », which is a feature removed, not filtered.

    The page says which is which, because unsaid the two read as a
    contradiction either way round: « Free est dit avoir 12 documents alors
    qu'en réalité il y en a plus » (owner, 20/09) was that misreading in the
    other direction. `test_panel_window.py` holds the panels' own tests; this
    is the sentence the list makes about them.
    """

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")

    def test_the_page_says_a_row_opens_on_the_same_dates(self):
        response = self.page(**FEBRUARY)
        self.assertContains(response, "ses achats sont ceux de ces dates")
        self.assertNotContains(response, "ses achats et sa courbe couvrent tout l'historique")

    def test_what_the_row_opens_really_is_those_dates(self):
        """Said and true: the row says 12 and the panel behind it holds the
        one delivery those 12 litres came from."""
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("12"))
        panel = self.client.get(
            reverse("inventory:stock_type_movements", args=[self.vodka.pk]), FEBRUARY
        )
        self.assertContains(panel, "10/02/2026")
        self.assertNotContains(panel, "10/01/2026")

    def test_nothing_is_said_when_no_window_is_asked_for(self):
        self.assertNotContains(self.page(), "ses achats sont ceux de ces dates")


class AnInventaireBeatsTheDatesTests(PurchaseWindowTestCase):
    """An inventaire is two physical counts with its own arithmetic -
    opening, closing, what is missing - which two free dates cannot
    produce. So it wins, and the page says the dates are not in force."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 1, 10), quantity="12", total_ht="240")
        self.take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(
            stock_take=self.take, product=None, stock_type=self.vodka,
            counted_quantity="4", unit=UnitChoices.LITRE,
        )

    def test_the_period_is_the_inventaire_and_the_january_purchase_is_back(self):
        response = self.page(inventaire=self.take.pk, **FEBRUARY)
        self.assertIsNotNone(response.context["period"])
        # The dates would have hidden this article; the inventaire does not.
        self.assertEqual(self.rows(response)["Vodka"]["period"].purchases, D("12"))
        self.assertEqual(response.context["column_count"], 10)

    def test_the_dates_are_disabled_and_the_page_says_why(self):
        response = self.page(inventaire=self.take.pk, **FEBRUARY)
        self.assertContains(response, 'name="du" value="2026-02-01" disabled')
        self.assertContains(response, 'name="au" value="2026-02-28" disabled')
        self.assertContains(response, "Les dates viennent de l'inventaire choisi")

    def test_depuis_le_debut_gives_the_dates_back(self):
        """The select carries the window as hidden fields, so the click that
        leaves the inventaire is not the click that loses the dates."""
        response = self.page(inventaire=self.take.pk, **FEBRUARY)
        self.assertContains(response, '<input type="hidden" name="du" value="2026-02-01">', html=False)
        self.assertContains(response, '<input type="hidden" name="au" value="2026-02-28">', html=False)


class ABadlyTypedWindowIsNotA500Tests(PurchaseWindowTestCase):
    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")

    def test_nonsense_dates_are_the_whole_page(self):
        for parameters in (
            {"du": "n-importe-quoi"},
            {"du": "hier", "au": "demain"},
            {"du": "2026-02-30"},
            {"du": "28/02/2026", "au": "01/02/2026"},
        ):
            with self.subTest(parameters=parameters):
                response = self.page(**parameters)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["date_window"])
                self.assertEqual(self.rows(response)["Vodka"]["quantity"], D("18"))

    def test_backwards_dates_are_swapped_rather_than_answered_empty(self):
        response = self.page(du="2026-02-28", au="2026-02-01")
        self.assertEqual(self.rows(response)["Vodka"]["quantity"], D("12"))

    def test_one_end_unreadable_leaves_the_other_standing(self):
        response = self.page(du="2026-02-01", au="pas-une-date")
        self.assertEqual(self.rows(response)["Vodka"]["quantity"], D("12"))


class TheWindowTravelsOnTheReloadTests(PurchaseWindowTestCase):
    """The list reloads itself in place after a product is classified, and
    `stock_catalogue` reads the request again - so anything missing from
    that URL is a window thrown away by the next click."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")

    def test_the_htmx_url_carries_the_two_dates(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(
            response.context["catalogue_url"],
            reverse("inventory:stock_catalogue") + "?du=2026-02-01&au=2026-02-28",
        )
        self.assertContains(response, "du=2026-02-01&amp;au=2026-02-28")

    def test_it_carries_the_inventaire_and_the_dates_together(self):
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        response = self.page(inventaire=take.pk, **FEBRUARY)
        self.assertEqual(
            response.context["catalogue_url"],
            reverse("inventory:stock_catalogue") + f"?inventaire={take.pk}&du=2026-02-01&au=2026-02-28",
        )

    def test_with_nothing_asked_it_stays_the_bare_url(self):
        self.assertEqual(
            self.page().context["catalogue_url"], reverse("inventory:stock_catalogue")
        )

    def test_the_reloaded_list_is_the_windowed_one(self):
        response = self.client.get(reverse("inventory:stock_catalogue"), FEBRUARY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(response)["Vodka"]["quantity"], D("12"))
        self.assertContains(response, "Du 01/02/2026 au 28/02/2026 :")
