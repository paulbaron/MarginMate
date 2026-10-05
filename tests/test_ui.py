"""Tests for the shared UI treatment.

The searching and sorting themselves are done in the browser by
static/js/datatable.js, which these can't execute. What they CAN pin down is
the contract that script relies on - and every one of these has a failure mode
that looks fine on a page you happen to be looking at and is broken on one you
aren't:

  * a table without `data-table` silently has no search box and no sortable
    headers, and nothing about the page looks wrong;
  * a date cell without `data-sort` sorts as the text "31/03/2026", so
    everything from March lands together regardless of year;
  * a continuation row without `data-child-row` is treated as a row in its own
    right and gets separated from the row it explains the moment you sort.
"""

import os
import pathlib
import re
import shutil
import tempfile
from collections import namedtuple
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from inventory.models import ShoppingExclusion, ShoppingListItem, StockType, UnitChoices
from recipes.sales import record_sales
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_priced_stock_type,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_supplier,
)
from tests.test_views_smoke import make_gaps_to_fill, make_shopping_history, make_shopping_lists


def add_bottles_of_gin(shopping_list):
    """Two 70 cl bottles of a gin on `shopping_list`, as « Ajouter » keeps
    them: the number counts the bottles, the size of one beside it
    (invented)."""
    return ShoppingListItem.objects.create(
        shopping_list=shopping_list,
        label="Gin exemple",
        quantity=Decimal("2"),
        product_name="GIN EXEMPLE 70CL X6",
        pack_size=6,
        item_size=Decimal("0.7"),
        size_unit=UnitChoices.LITRE,
    )


class BaseTemplateTests(TestCase):
    def test_the_table_script_is_loaded_everywhere(self):
        """Every enhanced table depends on it, so it belongs in the base
        template rather than being remembered per page."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "js/datatable.js")

    def test_the_stylesheet_is_loaded(self):
        self.assertContains(self.client.get(reverse("inventory:stock_list")), "css/marginmate.css")

    # Which navigation link lights up where: tests/test_navigation.py.


class SearchableSortableTableTests(TestCase):
    """Every list page gets a search box and sortable columns."""

    @classmethod
    def setUpTestData(cls):
        supplier = make_supplier(name="Metro")
        stock_type = make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        product = make_product(supplier=supplier, raw_name="VODKA 70CL", stock_type=stock_type)
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 31))
        make_invoice_line(invoice=invoice, product=product, quantity=6, total_ht="72")

        recipe = make_recipe(name="Vodka tonic", selling_price_ttc="8.50")
        make_ingredient(recipe, stock_type=stock_type, quantity="0.04", group=0)
        record_sales([("Vodka tonic", date(2026, 3, 15), 20)])

        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(stock_take=take, stock_type=stock_type, counted_quantity="4", unit=UnitChoices.LITRE)
        cls.take = take

    def assertEnhancedTable(self, url_name, **kwargs):
        response = self.client.get(reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "data-table",
            msg_prefix=f"{url_name} has a table with no search or sorting",
        )
        return response

    def test_invoice_list(self):
        self.assertEnhancedTable("invoices:invoice_list")

    def test_invoice_detail(self):
        from invoices.models import Invoice

        self.assertEnhancedTable("invoices:invoice_detail", pk=Invoice.objects.get().pk)

    def test_invoice_type_list(self):
        from tests.factories import make_invoice_type

        make_invoice_type(name="UBA")
        self.assertEnhancedTable("invoices:invoice_type_list")

    def test_supplier_list(self):
        self.assertContains(self.assertEnhancedTable("invoices:supplier_list"), 'data-table-label="fournisseurs"')

    def test_recipe_list(self):
        self.assertEnhancedTable("recipes:recipe_list")

    def test_sales_list(self):
        self.assertEnhancedTable("recipes:sales_list")

    def test_pos_product_list(self):
        from recipes.models import PosProduct

        PosProduct.objects.create(name="Mule", total_quantity=12)
        self.assertEnhancedTable("recipes:pos_product_list")

    def test_margins(self):
        """Its two tables are lists like any other - « which category
        makes the best margin » is a sort, and a page of thirteen rows
        without one is read line by line."""
        from datetime import timedelta

        from recipes.models import PosProduct, PosProductDailyQuantity

        product = PosProduct.objects.create(name="Mule", category="Cocktails", typology="Liquide (Alcool)")
        PosProductDailyQuantity.objects.create(
            product=product,
            # Inside the page's default period whenever this runs: dated in a
            # fixed month, the test stops covering the table a year later.
            sold_on=timezone.localdate() - timedelta(days=5),
            quantity=10,
            revenue_ttc=Decimal("85.00"),
            revenue_ht=Decimal("70.83"),
            revenue_read=True,
        )
        self.assertContains(self.assertEnhancedTable("margins:margins_home"), 'data-table-label="catégories"')

    def test_income(self):
        """« Entrées d'argent »: its means of payment, its months, its other
        entries, its deposits, its card payouts and its payers retained are
        lists like any other. Each of the last three is drawn only when
        there is something in it: a cash deposit, and a terminal's transfer
        counted as card through its payer."""
        from bank import income
        from bank.models import BankTransaction, IncomePayer

        # Inside the page's default period whenever this runs.
        today = timezone.localdate()
        BankTransaction.objects.create(
            operation_date=today,
            label="VIR SEPA RECU /FRM CLIENT EXEMPLE",
            amount=Decimal("50.00"),
            fingerprint="ui-income",
        )
        BankTransaction.objects.create(
            operation_date=today,
            bank_type="VERSEMENT ESPECES",
            label="VERSEMENT ESPECES",
            amount=Decimal("40.00"),
            fingerprint="ui-income-cash",
        )
        terminal = BankTransaction.objects.create(
            operation_date=today,
            label="VIR SEPA RECU /FRM TERMINAL EXEMPLE",
            counterparty="TERMINAL EXEMPLE",
            amount=Decimal("79.50"),
            fingerprint="ui-income-terminal",
        )
        IncomePayer.objects.create(key=income.payer_key(terminal), source=income.CARD)
        response = self.assertEnhancedTable("bank:income_home")
        for label in (
            "moyens de paiement",
            "mois",
            "catégories d'entrées",
            "autres entrées",
            "dépôts et autres moyens de paiement",
            "versements carte",
            "payeurs retenus",
        ):
            with self.subTest(label=label):
                self.assertContains(response, f'data-table-label="{label}"')

    def test_treasury(self):
        """« Trésorerie »: its months, its balances typed and its adjustments
        are lists like any other - the first two drawn once a balance is
        typed, the last once an adjustment exists. Dated from today, inside
        the page's default period whenever this runs. Invented."""
        from bank.models import BankTransaction, TreasuryAdjustment, TreasuryCheckpoint

        today = timezone.localdate()
        BankTransaction.objects.create(
            operation_date=today - timedelta(days=3),
            label="VIR EXEMPLE",
            amount=Decimal("50.00"),
            fingerprint="ui-treso",
        )
        TreasuryCheckpoint.objects.create(date=today - timedelta(days=10), balance=Decimal("100.00"))
        TreasuryCheckpoint.objects.create(date=today - timedelta(days=5), balance=Decimal("120.00"))
        TreasuryAdjustment.objects.create(date=today - timedelta(days=5), amount=Decimal("20.00"))
        response = self.assertEnhancedTable("bank:treasury")
        for label in ("mois", "soldes saisis", "ajustements"):
            with self.subTest(label=label):
                self.assertContains(response, f'data-table-label="{label}"')

    def test_staff(self):
        """« Personnel »: the employees and an employee's months are lists;
        the month's grid is a form, and is not one."""
        from staff.models import Employee

        # Invented: the repository is public and a timesheet is personal data.
        person = Employee.objects.create(last_name="Dupont", first_name="Jeanne", tuesday_hours=7)
        self.assertContains(self.assertEnhancedTable("staff:home"), 'data-table-label="salariés"')
        self.assertContains(self.assertEnhancedTable("staff:employee", pk=person.pk), 'data-table-label="mois"')

    def test_returnables(self):
        """« Consignes »: the pickups and the slips received are lists, and
        so are the slip formats and a slip's lines; the pickup's form is a
        form, and is not one."""
        from returnables.tests.support import make_pickup, make_slip

        make_pickup()
        slip = make_slip()
        response = self.assertEnhancedTable("returnables:home")
        self.assertContains(response, 'data-table-label="reprises"')
        self.assertContains(response, 'data-table-label="bons reçus"')
        self.assertContains(self.assertEnhancedTable("returnables:format_list"), 'data-table-label="formats de bons"')
        self.assertContains(
            self.assertEnhancedTable("returnables:slip_detail", pk=slip.pk), 'data-table-label="lignes du bon"'
        )

    def test_stock_take_list(self):
        self.assertEnhancedTable("inventory:stock_take_list")

    def test_stock_take_detail(self):
        self.assertEnhancedTable("inventory:stock_take_detail", pk=self.take.pk)

    def test_stock_gap_filler(self):
        """« Combler les écarts »: the sales to ring up, the amounts typed
        before them and the gaps are three lists like any other - their
        money sorts by a bare number, not by « 36.00 € » read as text, and
        when an amount was typed by a date that sorts as one."""
        take = make_gaps_to_fill()
        url = reverse("inventory:stock_gap_filler")

        def table(html, label):
            found = re.search(
                r'<table data-table data-table-label="' + label + r'"[^>]*>.*?</table>', html, flags=re.DOTALL
            )
            return found.group(0) if found else ""

        # Without a list, the gaps alone, still a list like any other.
        response = self.client.get(url, {"depuis": take.pk})
        self.assertContains(response, '<table data-table data-table-label="écarts"')
        self.assertNotContains(response, 'data-table-label="recettes à encaisser"')
        self.assertNotContains(response, 'data-table-label="montants déjà saisis"')
        # 70 € is a bottle of each wine; 36 €, on top of it, the red again.
        for amount in ("70", "36"):
            added = self.client.post(reverse("inventory:stock_gap_filler_add"), {"depuis": take.pk, "montant": amount})
            self.assertEqual(added.status_code, 302)
        response = self.client.get(url, {"depuis": take.pk})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, '<table data-table data-table-label="écarts"')
        latest = table(html, "recettes à encaisser")
        self.assertIn('data-sort="36.00"', latest)
        earlier = table(html, "montants déjà saisis")
        self.assertIn('data-sort="70.00"', earlier)
        self.assertRegex(earlier, r'<td data-sort="\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}">')

    def test_shopping_list_and_rhythm(self):
        """« Prévoir les courses »: each section a list like any other - the
        chance sorts by the bare figure it is drawn from, not « 98 % ·
        presque sûr » read as text, the money by its bare amount - and
        « Rythme d'achat »'s dates sort as dates."""
        made = make_shopping_history()
        html = self.assertEnhancedTable("inventory:shopping_list").content.decode()
        for label in (
            "à acheter",
            "peut-être",
            "nouveaux ici",
            "plus acheté",
            "acheté ailleurs",
            "les plus achetés ici",
        ):
            with self.subTest(table=label):
                self.assertIn(f'<table data-table data-table-label="{label}" class="phone-cards">', html)
        self.assertRegex(html, r'<td class="num" data-label="Chance" data-sort="0\.\d{4}">\d+ % · ')
        # « À acheter » sorts by its bare number, never by the grey note
        # under it (« 2 L », « pour 60 jours »).
        self.assertIn('<td class="num" data-label="À acheter" data-sort="24">24</td>', html)
        self.assertIn(
            '<td class="num" data-label="À acheter" data-sort="2">2<br><span class="muted small">2 L</span>', html
        )
        cells = re.findall(r'<td class="num" data-label="À acheter"[^>]*>', html)
        self.assertGreaterEqual(len(cells), 4)
        for cell in cells:
            self.assertRegex(cell, r'data-sort="[0-9]+(\.[0-9]+)?"')
        self.assertIn('data-label="Total HT" data-sort="1170.00"', html)
        response = self.client.get(reverse("inventory:shopping_rhythm"), {"fournisseur": made.wholesaler.pk})
        self.assertContains(response, '<table data-table data-table-label="rythme d\'achat" class="phone-cards">')
        last = timezone.localdate() - timedelta(days=7)
        self.assertContains(
            response, f'<td data-label="Dernier achat" data-sort="{last:%Y-%m-%d}">{last:%d/%m/%Y}</td>'
        )

    def test_shopping_lists(self):
        """« Listes de courses »: the lists open and finished, a store's list
        and a finished one are lists like any other - a date sorts as a
        date, « 1 / 3 » by its count, a quantity by its bare number, never
        « 2 L » read as text."""
        made = make_shopping_history()
        lists = make_shopping_lists(made)
        add_bottles_of_gin(lists.open)
        html = self.assertEnhancedTable("inventory:shopping_lists").content.decode()
        for label in ("listes en cours", "listes terminées"):
            with self.subTest(table=label):
                self.assertIn(f'<table data-table data-table-label="{label}" class="phone-cards">', html)
        started = timezone.localtime(lists.open.created_at)
        self.assertIn(f'<td data-label="Commencée le" data-sort="{started:%Y-%m-%d}">{started:%d/%m/%Y}</td>', html)
        self.assertIn('<td data-label="Pris" data-sort="1">1 / 4</td>', html)
        self.assertRegex(
            html, r'<td data-label="Terminée le" data-sort="\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}">\d{2}/\d{2}/\d{4}</td>'
        )
        page = reverse("inventory:shopping_list_page")
        response = self.client.get(page, {"fournisseur": made.wholesaler.pk})
        self.assertContains(response, '<table data-table data-table-label="articles" class="phone-cards">')
        self.assertContains(response, '<td class="num" data-label="Quantité" data-sort="2">2 L</td>')
        # Bottles of a size sort by their number, never by their words.
        self.assertContains(response, '<td class="num" data-label="Quantité" data-sort="2">2 bouteilles de 70 cl</td>')
        response = self.client.get(page, {"liste": lists.finished.pk})
        self.assertContains(response, '<table data-table data-table-label="courses terminées" class="phone-cards">')
        self.assertContains(response, '<td class="num" data-label="Quantité" data-sort="1">1 kg</td>')

    def test_stock_list_sorts_without_a_second_search_box(self):
        """It already has a server-backed fuzzy search; a second box filtering
        the same rows by a different rule would be worse than none."""
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-table-sort-only")
        self.assertContains(response, 'id="stock-search"')


class SortKeyTests(TestCase):
    """Cells whose displayed text doesn't sort correctly must say what does."""

    def test_invoice_dates_carry_an_iso_sort_key(self):
        supplier = make_supplier()
        product = make_product(supplier=supplier)
        for day in (date(2026, 3, 31), date(2025, 4, 1)):
            invoice = make_invoice(supplier=supplier, invoice_date=day)
            make_invoice_line(invoice=invoice, product=product, quantity=1, total_ht="10")

        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'data-sort="2026-03-31"')
        self.assertContains(response, 'data-sort="2025-04-01"')
        # ...and still READS as a French date.
        self.assertContains(response, "31/03/2026")

    def test_sale_dates_carry_an_iso_sort_key(self):
        make_recipe(name="Mule")
        record_sales([("Mule", date(2026, 3, 5), 4)])
        response = self.client.get(reverse("recipes:sales_list"))
        self.assertContains(response, 'data-sort="2026-03-05"')
        self.assertContains(response, "05/03/2026")

    def test_the_sold_column_carries_a_plain_numeric_sort_key(self):
        """Its displayed text can be "247.68" alone or "247.68 ?" with an
        estimate badge - sorting by the cell's own text (the datatable.js
        default) would fall back to a string compare for every row that
        has the badge, which sorted "247.68 ?" as text and mixed the
        estimated rows into the wrong order relative to the certain ones."""
        vodka = make_priced_stock_type(name="Vodka", unit_cost_ht="15", quantity="10")
        gin = make_priced_stock_type(name="Gin", unit_cost_ht="25", quantity="10")
        recipe = make_recipe(name="Mule")
        make_ingredient(recipe, stock_type=vodka, quantity="0.04", group=0)
        make_ingredient(recipe, stock_type=gin, quantity="0.04", group=0)
        record_sales([("Mule", date(2026, 3, 5), 100)])

        response = self.client.get(reverse("inventory:stock_list"))
        # Gin is the priciest alternative, so it's the one carrying the
        # estimate - and its sort key must still be the bare number.
        #
        # Four decimals rather than two since 25/09/2026, and the same
        # number: what one sale consumes is scaled by `Recipe.sold_share`
        # (a multiplication) where it used to be divided by the yield, and
        # Decimal division normalises an exponent where multiplication adds
        # it - 0,0400 x 100 is 4,0000 where 0,0400 / 1,0000 x 100 was 4,00.
        # Nobody reads this: the cell itself prints |floatformat:2, and
        # datatable.js parses the attribute as a number. What this test is
        # about is that the attribute is a bare number at all, with none of
        # the « ? » the estimate badge puts in the cell's own text.
        self.assertContains(response, 'data-sort="4.0000"')

    def test_pickup_and_slip_dates_carry_an_iso_sort_key(self):
        """« Consignes »: a pickup's day and a slip's delivery sort by their
        ISO date - and read as French dates."""
        from returnables.tests.support import make_pickup, make_slip

        make_pickup(date=date(2026, 3, 31))
        make_pickup(date=date(2025, 4, 1))
        make_slip(delivery_date=date(2026, 2, 5))
        response = self.client.get(reverse("returnables:home"))
        for key, shown in (("2026-03-31", "31/03/2026"), ("2025-04-01", "01/04/2025"), ("2026-02-05", "05/02/2026")):
            with self.subTest(day=key):
                self.assertContains(response, f'data-sort="{key}"')
                self.assertContains(response, shown)


class GroupedAmountSortTests(SimpleTestCase):
    """An amount a page prints has its thousands grouped by a no-break space
    (« 1 234.56 € », common.THOUSANDS_SEPARATOR). datatable.js sorts a cell
    by its own text, so its number parser must take every white space out
    before it parses - `\\s`, which in JavaScript matches U+00A0 and U+202F
    as well. Kept to plain spaces, every amount over a thousand would sort
    as text."""

    def test_the_number_parser_takes_every_white_space_out_first(self):
        source = (pathlib.Path(__file__).resolve().parent.parent / "static/js/datatable.js").read_text(encoding="utf-8")
        body = source[source.index("function asNumber(text)") : source.index("function asDate(text)")]
        stripped = re.search(r'\.replace\(/\[([^\]]*)\]/g, ""\)', body)
        self.assertIsNotNone(stripped, "asNumber no longer strips a class of spaces")
        self.assertIn(r"\s", stripped.group(1))
        self.assertLess(stripped.start(), body.index("parseFloat"))

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent / "static/js/datatable.js").read_text(encoding="utf-8")

    def test_the_search_finds_a_grouped_amount_typed_either_way(self):
        """« 1 234.56 € » was found by typing « 1234.56 » before amounts were
        grouped. normalize(), which the box's text and the row's both go
        through, takes the space between a digit and a group of three out -
        whichever space, since a no-break one copies out as an ordinary one."""
        source = self.source()
        body = source[source.index("function normalize(text)") : source.index("function searchableText")]
        self.assertIn('.replace(SPACED_THOUSANDS, "$1")', body)
        # The JavaScript literal reads the same under Python's re (\s takes
        # the no-break spaces in both).
        pattern = re.search(r"var SPACED_THOUSANDS = /(.+)/g;", source).group(1)
        nbsp = "\N{NO-BREAK SPACE}"
        for text, folded in (
            (f"16{nbsp}568{nbsp}684.50 €", "16568684.50 €"),
            ("1 408.18", "1408.18"),
            (f"1{nbsp}408", "1408"),
            ("pack 6 33cl", "pack 6 33cl"),
            ("1 40", "1 40"),
            ("1 4000", "1 4000"),
        ):
            with self.subTest(text=text):
                self.assertEqual(re.sub(pattern, r"\1", text), folded)

    def test_the_collator_never_reads_a_grouped_amount_as_two_numbers(self):
        """A cell that is not a bare number (« 999.00 – 1 000.00 € ») is
        compared by the numeric collator, which reads « 1 000 » as 1 and 0:
        the grouping spaces - and only those - go before it."""
        source = self.source()
        declaration = re.search(r"var GROUPING_SPACE = [^\n]*", source).group(0)
        self.assertIn("0x00a0", declaration)
        self.assertIn("0x202f", declaration)
        body = source[source.index("function cellText(cell)") : source.index("function asNumber(text)")]
        self.assertLess(body.index('.replace(GROUPING_SPACE, "$1")'), body.index(r".replace(/\s+/g"))


class ChildRowTests(TestCase):
    """Rows that explain the row above them have to travel with it."""

    def test_stock_list_detail_rows_are_marked_as_children(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "data-child-row")

    def test_the_variance_valuation_note_is_marked_as_a_child(self):
        """ "Valorisé en Rhum (…)" belongs to the pool row above it; sorted
        apart, it would sit under an unrelated group and read as its
        valuation."""
        supplier = make_supplier()
        stock_type = make_priced_stock_type(name="Rhum", unit_cost_ht="15", quantity="100")
        make_product(supplier=supplier, stock_type=stock_type, unit=UnitChoices.UNIT, stock_equivalent="0.7")

        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        make_stock_take_line(stock_take=opening, stock_type=stock_type, counted_quantity="20", unit=UnitChoices.LITRE)
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        make_stock_take_line(stock_take=closing, stock_type=stock_type, counted_quantity="15", unit=UnitChoices.LITRE)

        response = self.client.get(reverse("inventory:stock_take_variance", kwargs={"pk": closing.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-child-row")

    def test_stock_list_child_row_colspan_matches_the_header(self):
        """A child row's colspan has to cover every column, or table-layout
        spreads the mismatch across every row that shares those columns -
        not just the one that got expanded. Stayed at 6 when "Vendu" made
        this a 7-column table, which is what made expanding any row widen
        the whole category."""
        import re

        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        html = self.client.get(reverse("inventory:stock_list")).content.decode()

        thead = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)
        header_columns = thead.count("<th")
        self.assertGreater(header_columns, 0)

        child_colspans = re.findall(r'<tr[^>]*data-child-row[^>]*>\s*<td colspan="(\d+)"', html)
        self.assertTrue(child_colspans, "expected at least one data-child-row with a colspan")
        for colspan in child_colspans:
            self.assertEqual(int(colspan), header_columns)

    def test_stock_list_column_widths_are_wide_enough_for_their_content(self):
        """The category table is table-layout:fixed (see marginmate.css) so
        a nested purchase-history table can't force its columns wider on
        expand - which means the <colgroup> is now what decides column
        widths, and a gap here is silent: too narrow and "Modifier" / the
        price-history button / "Supprimer" wrap onto extra lines, which is
        what made every row noticeably taller than it needs to be."""
        import re

        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        html = self.client.get(reverse("inventory:stock_list")).content.decode()

        # Shares of the width: beside the panel of products to classify the
        # list is about 880px wide, alone up to 1190px.
        shares = [float(w) for w in re.findall(r'<col style="width:(\d+)%">', html)]
        self.assertEqual(len(shares), 7)
        self.assertEqual(sum(shares), 100)
        type_width, _qty, _sold, _unit, _ht, _ttc, actions_width = (880 * share / 100 for share in shares)
        # "Modifier" + the 📈 button + "Supprimer", each with their own
        # margin, need roughly 190px on one line - see the row-height test.
        self.assertGreaterEqual(actions_width, 190)
        self.assertGreaterEqual(type_width, 200)


class PageChromeTests(TestCase):
    def test_list_pages_explain_themselves(self):
        """A subtitle saying what the page is for - this is a tool someone
        comes back to monthly, not daily."""
        for url_name in (
            "inventory:stock_list",
            "invoices:invoice_list",
            "recipes:recipe_list",
            "recipes:sales_list",
            "recipes:auto_sales",
            "inventory:stock_take_list",
            # No count in this database: its empty state still says it.
            "inventory:stock_gap_filler",
            # Nothing bought in this database: both say what they are for all the same.
            "inventory:shopping_list",
            "inventory:shopping_rhythm",
            "inventory:shopping_lists",
            "margins:margins_home",
            "bank:income_home",
            # With no balance typed yet: its subtitle says it all the same.
            "bank:treasury",
            "bank:recognition",
            "bank:statement_formats",
            "staff:home",
            "returnables:home",
            "returnables:format_list",
            "returnables:type_list",
            "notifications:home",
            "notifications:reminders",
            "notifications:events",
        ):
            with self.subTest(page=url_name):
                self.assertContains(self.client.get(reverse(url_name)), "page-subtitle")

    def test_the_gap_filler_explains_itself_with_a_list(self):
        """With a count and a list, not only in its empty state: the
        subtitle says what the page is for, amount after amount."""
        take = make_gaps_to_fill()
        added = self.client.post(reverse("inventory:stock_gap_filler_add"), {"depuis": take.pk, "montant": "70"})
        self.assertEqual(added.status_code, 302)
        response = self.client.get(reverse("inventory:stock_gap_filler"), {"depuis": take.pk})
        self.assertContains(response, 'id="a-encaisser"')
        self.assertContains(
            response,
            '<p class="page-subtitle">Les ventes à encaisser pour réduire chaque écart de stock dans la même '
            "proportion, montant après montant.</p>",
            count=1,
        )

    def test_the_shopping_list_explains_itself_with_a_list(self):
        """With purchases, not only in its empty state: one subtitle, the
        figures at its head, and « Comment c'est calculé » folded."""
        make_shopping_history()
        response = self.client.get(reverse("inventory:shopping_list"))
        self.assertContains(
            response,
            '<p class="page-subtitle">Ce que vous rachetez d\'habitude dans une enseigne, sans inventaire.</p>',
            count=1,
        )
        self.assertContains(response, 'class="stat-row"')
        self.assertContains(response, "<summary>Comment c'est calculé</summary>")
        response = self.client.get(reverse("inventory:shopping_rhythm"))
        self.assertContains(
            response, '<p class="page-subtitle">Tous vos articles : à quel rythme et où vous les achetez.</p>', count=1
        )

    def test_the_stock_page_leads_with_its_headline_figures(self):
        make_priced_stock_type(name="Vodka", unit_cost_ht="12", quantity="10")
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(response, "stat-value")
        self.assertContains(response, "Total acheté (HT)")

    def test_an_empty_list_offers_the_next_step(self):
        """An empty state that only says "nothing here" leaves the reader to
        work out what to do about it."""
        response = self.client.get(reverse("recipes:recipe_list"))
        self.assertContains(response, "empty-state")
        self.assertContains(response, reverse("recipes:recipe_create"))

    def test_action_buttons_use_the_shared_class(self):
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(response, 'class="actions"')
        self.assertNotContains(response, 'style="display:flex; gap:0.5rem;"')


class TemplateHygieneTests(TestCase):
    """Static checks over the templates themselves.

    Django's `{# … #}` comment is SINGLE-LINE ONLY. Spread one over several
    lines and Django doesn't treat it as a comment at all - it renders the
    whole thing to the page as visible text. It looks completely normal in the
    editor, which is why it has now shipped twice.
    """

    def template_files(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        return [path for path in root.rglob("*.html") if ".venv" not in path.parts and "staticfiles" not in path.parts]

    def test_no_multiline_hash_comments(self):
        import re

        offenders = []
        for path in self.template_files():
            for match in re.finditer(r"\{#.*?#\}", path.read_text(encoding="utf-8"), re.DOTALL):
                if "\n" in match.group(0):
                    offenders.append(f"{path.name}: {match.group(0)[:60]}…")
        self.assertEqual(
            offenders,
            [],
            "Multi-line {# #} renders as visible text - use {% comment %} instead:\n" + "\n".join(offenders),
        )

    def test_every_template_is_syntactically_valid(self):
        """A template that only renders on one rarely-visited page still
        fails at import time here rather than in front of someone."""
        import pathlib

        from django.template import TemplateSyntaxError
        from django.template.loader import get_template

        root = pathlib.Path(__file__).resolve().parent.parent
        for path in self.template_files():
            # Name it the way the loader will look it up.
            for base in (
                "templates",
                *[
                    f"{app}/templates"
                    for app in (
                        "accounts",
                        "inventory",
                        "invoices",
                        "recipes",
                        "bank",
                        "margins",
                        "transfer",
                        "staff",
                        "returnables",
                        "notifications",
                    )
                ],
            ):
                candidate = root / base
                if candidate in path.parents:
                    name = str(path.relative_to(candidate)).replace("\\", "/")
                    break
            else:
                continue
            with self.subTest(template=name):
                try:
                    get_template(name)
                except TemplateSyntaxError as exc:
                    self.fail(f"{name}: {exc}")


class ReturnablesWritesNoMarkupTests(TestCase):
    """« Consignes » prints what a stranger wrote: a type's name, a pattern, a
    slip's designations, a mail's subject, a photo's file name. Every
    sentence its pure modules build is plain text, escaped by the templates -
    nothing in the app may turn text into markup, and its script builds
    nodes with textContent (the rule charts.js was caught breaking)."""

    def test_no_safe_filter_nor_mark_safe_in_the_app(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "returnables"
        offenders = []
        for path in root.rglob("*.html"):
            # What a comment says about |safe is not a use of it.
            text = re.sub(
                r"\{% comment %\}.*?\{% endcomment %\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL
            )
            for pattern in (r"\|\s*safe(seq)?\b", r"\{%\s*autoescape\s+off"):
                if re.search(pattern, text):
                    offenders.append(f"{path.name}: {pattern}")
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in (
                r"\bmark_safe\s*\(",
                r"\bSafeString\s*\(",
                r"django\.utils\.safestring",
                r"\bformat_html\s*\(",
            ):
                if re.search(pattern, text):
                    offenders.append(f"{path.name}: {pattern}")
        self.assertEqual(offenders, [])

    def test_the_returnables_script_never_writes_markup(self):
        """photos.js too: the photos' previews moved there (01/10), shared
        with Achats' « Prendre une photo », and they print a file's name."""
        import pathlib
        import re

        for name in ("returnables.js", "photos.js"):
            source = (pathlib.Path(__file__).resolve().parent.parent / "static/js" / name).read_text(encoding="utf-8")
            for pattern in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML", r"document\.write"):
                with self.subTest(script=name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source))

    def test_no_inline_script_nor_style_in_the_app_s_templates(self):
        """A strict Content-Security-Policy is planned: the page's script is
        static/js/returnables.js, its looks marginmate.css."""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "returnables" / "templates"
        for path in root.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertIsNone(re.search(r"\sstyle=", text))
                self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", text))
                self.assertIsNone(re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", text))

    def test_the_automatic_sales_page_has_no_inline_script_nor_style(self):
        """« Import automatique des ventes » (recipes/auto_sales.html and its
        fields), written for the strict policy the notifications' pages
        follow."""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "recipes" / "templates" / "recipes"
        for name in ("auto_sales.html", "_auto_sales_fields.html"):
            text = (root / name).read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertIsNone(re.search(r"\sstyle=", text))
                self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", text))
                self.assertIsNone(re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", text))

    def test_the_gather_card_the_page_includes_has_no_inline_style(self):
        """/consignes/ draws Achats' gather card (invoices/_gather_status.html)
        while a gather of slips runs: the CSP the page is written for covers
        what it includes too. Its spacing was four inline style= (29/09)."""
        import pathlib
        import re

        path = pathlib.Path(__file__).resolve().parent.parent / "invoices/templates/invoices/_gather_status.html"
        text = path.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\sstyle=", text))
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", text))


class ReturnablesStylesheetTests(TestCase):
    def test_the_phone_rules_are_in_the_stylesheet(self):
        """The fast loop's guard of what returnables/tests/test_phone_browser.py
        measures in Chrome: every small button of these pages 44 px tall (a
        photo's « Retirer » was 30, « Effacer » a line of text), and a file
        name without spaces wrapped rather than widening the page (in a
        slip's subtitle, in a message - which sits outside the page's root)."""
        import pathlib

        css = (pathlib.Path(__file__).resolve().parent.parent / "static/css/marginmate.css").read_text(encoding="utf-8")
        block = css[css.index("/* ----------------------------------------------------------- returnables */") :]
        for selector, declaration in (
            (r"\.returnables-page \.btn-small", r"min-height:\s*44px"),
            (r"\.draft-notice \.link-button", r"min-height:\s*44px"),
            (r"\.photo-remove > summary", r"min-height:\s*44px"),
            (r"\.returnables-page \.page-subtitle", r"overflow-wrap:\s*anywhere"),
            (r"html:has\(\.returnables-page\) \.message", r"overflow-wrap:\s*anywhere"),
        ):
            with self.subTest(selector=selector):
                self.assertRegex(block, selector + r"\s*\{[^}]*" + declaration)


class JobConsoleTests(TestCase):
    """The shared console partial. Both job kinds render through it, so the
    log stops being two near-identical blocks of markup that drift apart."""

    def render(self, **job_kwargs):
        from django.template.loader import render_to_string

        from invoices.models import ScrapeJob

        job = ScrapeJob.objects.create(**job_kwargs)
        job.log = "[+  0.1s] Connexion\n[+  4.2s] 12 175 emails analysés\n"
        job.save(update_fields=["log"])
        return render_to_string(
            "_job_console.html",
            {
                "log": job.log,
                "running": job.is_active,
                "last_line": job.last_log_line,
                "log_lines": job.log_lines,
                "console_id": "test-log",
            },
        ), job

    def test_the_log_is_collapsed_behind_a_disclosure(self):
        """A wall of scanner output as the first thing on the page is what
        made these alarming."""
        html, _job = self.render(status="SUCCESS")
        self.assertIn("<details", html)
        self.assertNotIn("<details open", html)
        self.assertIn("Journal technique", html)

    def test_the_full_log_is_still_there(self):
        html, _job = self.render(status="SUCCESS")
        self.assertIn("12 175 emails analysés", html)

    def test_it_counts_the_lines(self):
        html, _job = self.render(status="SUCCESS")
        self.assertIn("2 lignes", html)

    def test_a_running_job_shows_its_last_line_in_plain_sight(self):
        html, _job = self.render(status="RUNNING")
        self.assertIn("job-spinner", html)
        self.assertIn("12 175 emails analysés", html.split("<details")[0])

    def test_a_finished_job_shows_no_spinner(self):
        html, _job = self.render(status="SUCCESS")
        self.assertNotIn("job-spinner", html)

    def test_the_elapsed_prefix_is_stripped_from_the_live_line(self):
        """ "[+  4.2s]" is useful in the log and noise on the one line shown
        as a live status, where the spinner already says "running"."""
        _html, job = self.render(status="RUNNING")
        self.assertEqual(job.last_log_line, "12 175 emails analysés")

    def test_nothing_renders_without_a_log(self):
        from django.template.loader import render_to_string

        self.assertEqual(render_to_string("_job_console.html", {"log": ""}).strip(), "")

    def test_both_job_models_answer_the_same_questions(self):
        """The partial is shared, so ScrapeJob and SalesImportJob both have
        to expose is_active / log_lines / last_log_line."""
        from invoices.models import ScrapeJob
        from recipes.models import SalesImportJob

        for model in (ScrapeJob, SalesImportJob):
            with self.subTest(model=model.__name__):
                job = model.objects.create(status="RUNNING")
                job.log = "[+  1.0s] une ligne\n"
                self.assertTrue(job.is_active)
                self.assertEqual(job.log_lines, 1)
                self.assertEqual(job.last_log_line, "une ligne")


class PersistedStateTests(TestCase):
    """Markup that tells ui.js what to remember across a reload."""

    def test_the_invoice_sources_remember_their_ticks(self):
        response = self.client.get(reverse("invoices:invoice_list"))
        html = response.content.decode()
        self.assertIn("data-persist-durable", html)
        self.assertIn('data-persist="sources-open"', html)

    def test_a_source_checkbox_carries_its_own_key(self):
        from tests.factories import make_supplier

        make_supplier(code="METRO", name="Metro", parser_key="METRO", is_scrapable=True)
        html = self.client.get(reverse("invoices:invoice_list")).content.decode()
        self.assertIn('data-persist="source:METRO"', html)


class ChartMarkupTests(TestCase):
    """The charts are server-rendered SVG and readable without JavaScript;
    charts.js only adds the hover readout. It needs the values in the markup
    to do that - the browser's own <title> tooltip is too slow to be useful."""

    def test_the_pie_carries_a_label_and_value_per_slice(self):
        from decimal import Decimal

        from recipes.views import _build_ingredient_pie_svg

        class FakeIngredient:
            source_name = "Vodka"

        html = _build_ingredient_pie_svg(
            [
                {"ingredient": FakeIngredient(), "name": "Vodka", "cost_ht": Decimal("3")},
                {"ingredient": FakeIngredient(), "name": "Gin", "cost_ht": Decimal("1")},
            ]
        )
        self.assertIn('data-chart="pie"', html)
        self.assertIn('data-label="Vodka"', html)
        self.assertIn("75.0 %", html)
        self.assertIn("chart-legend", html)

    def test_a_single_ingredient_is_drawn_as_a_full_circle(self):
        """An arc from a point back to itself collapses to nothing."""
        from decimal import Decimal

        from recipes.views import _build_ingredient_pie_svg

        class FakeIngredient:
            source_name = "Vodka"

        html = _build_ingredient_pie_svg([{"ingredient": FakeIngredient(), "name": "Vodka", "cost_ht": Decimal("3")}])
        self.assertIn("<circle", html)
        self.assertIn("100.0 %", html)

    def test_slice_names_are_escaped(self):
        """Rendered with |safe, and the names come from invoice text."""
        from decimal import Decimal

        from recipes.views import _build_ingredient_pie_svg

        class FakeIngredient:
            source_name = "<img src=x onerror=alert(1)>"

        html = _build_ingredient_pie_svg([{"ingredient": FakeIngredient(), "cost_ht": Decimal("3")}])
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_the_price_chart_carries_a_point_per_reading(self):
        from datetime import date
        from decimal import Decimal

        from inventory.views import _build_price_history_svg

        html = _build_price_history_svg([(date(2026, 1, 1), Decimal("1.50")), (date(2026, 2, 1), Decimal("1.80"))])
        self.assertIn('data-chart="line"', html)
        self.assertEqual(html.count("chart-point"), 2)
        self.assertIn('data-label="01/01/2026"', html)
        self.assertIn("chart-hover-line", html)

    def test_every_css_variable_used_anywhere_is_defined(self):
        """A var() that resolves to nothing doesn't error - it just silently
        draws nothing, which is how the price chart's axes went invisible
        after the stylesheet renamed a token."""
        import glob
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        css = (root / "static/css/marginmate.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"(--[\w-]+)\s*:", css))

        used = {}
        patterns = ("static/css/*.css", "static/js/*.js", "templates/**/*.html", "*/templates/**/*.html", "*/views.py")
        for pattern in patterns:
            for path in glob.glob(str(root / pattern), recursive=True):
                if ".venv" in path:
                    continue
                for name in re.findall(r"var\((--[\w-]+)", pathlib.Path(path).read_text(encoding="utf-8")):
                    used.setdefault(name, set()).add(pathlib.Path(path).name)

        missing = {name: sorted(files) for name, files in used.items() if name not in defined}
        self.assertEqual(missing, {}, f"CSS variables used but never defined: {missing}")

    def test_the_axes_use_a_colour_that_actually_exists(self):
        """They were drawn with var(--panel-border) after the stylesheet
        renamed it, which made them invisible."""
        import pathlib
        import re
        from datetime import date
        from decimal import Decimal

        from inventory.views import _build_price_history_svg

        html = _build_price_history_svg([(date(2026, 1, 1), Decimal("1.50")), (date(2026, 2, 1), Decimal("1.80"))])
        css = (pathlib.Path(__file__).resolve().parent.parent / "static/css/marginmate.css").read_text(encoding="utf-8")
        for name in set(re.findall(r"var\((--[\w-]+)\)", html)):
            with self.subTest(variable=name):
                self.assertIn(name + ":", css, f"{name} is used by a chart but not defined in the CSS")

    def test_too_few_points_render_nothing(self):
        from datetime import date
        from decimal import Decimal

        from inventory.views import _build_price_history_svg

        self.assertEqual(_build_price_history_svg([(date(2026, 1, 1), Decimal("1.50"))]), "")

    def test_the_hover_script_never_writes_markup(self):
        """The server escapes every name it puts in `data-label`, and
        `getAttribute` hands the name back DECODED: written into the tooltip
        as HTML, a category typed as `<img onerror=…>` ran on hover. The
        tooltip is built from nodes and text only
        (PieTooltipInBrowserTests drives it in a real Chrome)."""
        import pathlib
        import re

        source = (pathlib.Path(__file__).resolve().parent.parent / "static/js/charts.js").read_text(encoding="utf-8")
        for pattern in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML", r"document\.write"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))


class FormRenderingTests(TestCase):
    """Every form goes through templates/_form_fields.html.

    The field loop used to be copy-pasted into seven templates and had
    drifted: some rendered each field's help text, some didn't - and the ones
    that didn't were the recipe and stock-item forms, where the models define
    the most useful help ("Ex : 0.20 pour 20%").
    """

    FORM_PAGES = [
        ("inventory:stock_type_create", {}),
        ("inventory:stock_take_create", {}),
        ("invoices:invoice_upload", {}),
        ("invoices:invoice_create_manual", {}),
        ("invoices:invoice_type_create", {}),
        ("recipes:recipe_create", {}),
        ("recipes:auto_sales", {}),
    ]

    def test_every_form_page_uses_the_shared_layout(self):
        for name, kwargs in self.FORM_PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name, kwargs=kwargs)).content.decode()
                self.assertIn("form-grid", html)

    def test_no_page_waits_on_a_third_party_host(self):
        """Every asset is served from this machine.

        htmx used to come from unpkg.com, which answers with a 301 before the
        real file - so every page spent two third-party round trips before
        any of the app's OWN scripts ran, because deferred scripts run in
        document order. A bar's back office should also work with the
        internet down.
        """
        html = self.client.get(reverse("inventory:stock_list")).content.decode()
        head = html.split("</head>")[0]
        for external in ("//unpkg.com", "//cdn.", "//cdnjs.", "//ajax.googleapis.com"):
            self.assertNotIn(external, head, f"{external} is back in the page head")

    def test_the_head_holds_no_parser_blocking_script(self):
        """`defer` does nothing on an INLINE script: it runs where it sits,
        and a classic script waits for pending stylesheets first - so one in
        the head stops the page being parsed at all until the CSS lands."""
        html = self.client.get(reverse("inventory:stock_list")).content.decode()
        head = html.split("</head>")[0]
        inline_scripts = [block for block in head.split("<script")[1:] if not block.lstrip().startswith("src=")]
        self.assertEqual(inline_scripts, [], "inline <script> in <head> blocks parsing on the stylesheet")

    def test_the_head_waits_for_one_script_only_topbar_js(self):
        """static/js/topbar.js is loaded without defer on purpose (30/09):
        the class it puts on <html> is what folds the topbar's links into
        « Menu » under 860 px, and deferred like the others a phone painted
        three rows of links and then jumped. It is the only one - every other
        script of the head waits for the page (base.html says why)."""
        html = self.client.get(reverse("inventory:stock_list")).content.decode()
        head = html.split("</head>")[0]
        waiting = [
            opening
            for opening in re.findall(r"<script\b[^>]*>", head)
            if not re.search(r"\s(defer|async)\b", opening) and 'type="module"' not in opening
        ]
        self.assertEqual(len(waiting), 1, waiting)
        self.assertRegex(waiting[0], r'\ssrc="[^"]*js/topbar\.js\?v=')

    def test_no_template_still_hand_rolls_a_field_loop(self):
        """`{{ field.label_tag }}` was the giveaway of the copy-pasted block.
        Looked for literally rather than by parsing loops: a cleverer check
        kept flagging `{% for error in line_form.errors %}`, which is a
        different job and perfectly fine."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = [
            path.name
            for path in root.rglob("*.html")
            if ".venv" not in path.parts and "label_tag" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"These still render fields by hand: {offenders}")

    def test_help_text_reaches_the_page(self):
        html = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertIn("Ex : 0.20 pour 20%", html)
        self.assertIn('class="help"', html)

    def test_required_fields_are_marked(self):
        html = self.client.get(reverse("recipes:recipe_create")).content.decode()
        self.assertIn('class="required"', html)

    def test_a_field_with_an_error_is_flagged(self):
        response = self.client.post(reverse("inventory:stock_type_create"), {"name": "", "unit": ""})
        self.assertContains(response, "has-error")
        self.assertContains(response, "field-error")

    def test_hidden_fields_are_rendered_but_not_labelled(self):
        """A hidden input inside a .form-field would leave an empty labelled
        box on the page."""
        from django import forms
        from django.template.loader import render_to_string

        class Sample(forms.Form):
            visible = forms.CharField()
            secret = forms.CharField(widget=forms.HiddenInput())

        html = render_to_string("_form_fields.html", {"form": Sample()})
        self.assertIn('name="secret"', html)
        self.assertEqual(html.count("form-field "), html.count("form-field form-field-text"))

    def test_excluded_fields_are_left_out(self):
        """The invoice-type page renders its two date fields beside the
        "Tester" button instead of among the pattern fields."""
        from django import forms
        from django.template.loader import render_to_string

        class Sample(forms.Form):
            keep = forms.CharField()
            drop = forms.CharField()

        html = render_to_string("_form_fields.html", {"form": Sample(), "exclude": ["drop"]})
        self.assertIn('name="keep"', html)
        self.assertNotIn('name="drop"', html)


class AssetVersioningTests(TestCase):
    """`{% asset %}` stamps the file's mtime onto its URL.

    Without it, editing the stylesheet and seeing nothing change looks like
    the change didn't work rather than like it didn't load - which cost two
    debugging detours during this very pass.
    """

    def test_the_url_carries_a_version(self):
        from inventory.templatetags.assets import asset

        url = asset("css/marginmate.css")
        self.assertIn("marginmate.css?v=", url)

    def test_a_missing_file_still_yields_a_usable_url(self):
        from inventory.templatetags.assets import asset

        self.assertEqual(asset("css/does-not-exist.css"), "/static/css/does-not-exist.css")

    def test_where_a_file_is_is_remembered_its_date_is_not(self):
        """The finders walk every static folder; the answer (a path) is kept
        for the process, the file's date is read at every call: an edit
        still changes the address at once."""
        from django.contrib.staticfiles import finders

        from inventory.templatetags import assets

        folder = pathlib.Path(tempfile.mkdtemp(prefix="marginmate-tests-assets-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "css").mkdir()
        sheet = folder / "css" / "essai-cache.css"
        sheet.write_text("a{}", encoding="utf-8")
        os.utime(sheet, (1_700_000_000, 1_700_000_000))
        with self.settings(STATICFILES_DIRS=[folder]):
            self.assertEqual(assets.asset("css/essai-cache.css"), "/static/css/essai-cache.css?v=1700000000")
            os.utime(sheet, (1_700_000_100, 1_700_000_100))
            with mock.patch.object(finders, "find", side_effect=AssertionError("looked for again")):
                self.assertEqual(assets.asset("css/essai-cache.css"), "/static/css/essai-cache.css?v=1700000100")
            # Gone from there: looked for again, and nothing is not remembered.
            sheet.unlink()
            self.assertEqual(assets.asset("css/essai-cache.css"), "/static/css/essai-cache.css")
            self.assertNotIn("css/essai-cache.css", assets._FOUND)
            sheet.write_text("a{}", encoding="utf-8")
            os.utime(sheet, (1_700_000_200, 1_700_000_200))
            self.assertEqual(assets.asset("css/essai-cache.css"), "/static/css/essai-cache.css?v=1700000200")
        # Other static folders: looked for again.
        self.assertNotIn("css/essai-cache.css", assets._FOUND)
        self.assertEqual(assets.asset("css/essai-cache.css"), "/static/css/essai-cache.css")

    def test_the_base_template_uses_it_for_every_local_asset(self):
        html = self.client.get(reverse("inventory:stock_list")).content.decode()
        # topbar.js (30/09): the one script of the head, « Menu » on a phone.
        for name in ("marginmate.css", "topbar.js", "ui.js", "datatable.js", "charts.js"):
            with self.subTest(asset=name):
                self.assertIn(name + "?v=", html)

    def collected(self):
        """A STATIC_ROOT holding a copy of marginmate.css dated 1700000000,
        as `serve`'s collectstatic leaves it, under the server's settings
        (WhiteNoise serving that copy, indexed once)."""
        from django.conf import settings

        root = pathlib.Path(tempfile.mkdtemp(prefix="marginmate-tests-collected-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "css").mkdir()
        copy = root / "css" / "marginmate.css"
        shutil.copyfile(pathlib.Path(settings.BASE_DIR) / "static" / "css" / "marginmate.css", copy)
        os.utime(copy, (1_700_000_000, 1_700_000_000))
        served = self.settings(STATIC_ROOT=root, WHITENOISE_AUTOREFRESH=False, WHITENOISE_USE_FINDERS=False)
        served.enable()
        self.addCleanup(served.disable)
        return copy

    def test_on_the_server_it_dates_the_copy_served_once(self):
        """`serve` serves the copy collectstatic made at its start, not the
        source: a source edited while it runs (a pull before the restart)
        got a new `?v=` over the old bytes - now kept a year by the browsers
        (VersionedWhiteNoiseMiddleware). The copy's date is read once."""
        from inventory.templatetags import assets

        copy = self.collected()
        self.assertEqual(assets.asset("css/marginmate.css"), "/static/css/marginmate.css?v=1700000000")
        os.utime(copy, (1_700_000_100, 1_700_000_100))
        with mock.patch.object(os.path, "getmtime", side_effect=AssertionError("dated again")):
            self.assertEqual(assets.asset("css/marginmate.css"), "/static/css/marginmate.css?v=1700000000")

    def test_on_the_server_a_file_with_no_copy_is_looked_for_there_once(self):
        """A file added after the start's collectstatic has no copy to date,
        and WhiteNoise, which indexed STATIC_ROOT once, will not serve one
        made later: looking for it again at every page found nothing."""
        from config.static import collected_version

        self.collected()
        self.assertIsNone(collected_version("css/essai-absent.css"))
        with mock.patch.object(os.path, "getmtime", side_effect=AssertionError("looked for again")):
            self.assertIsNone(collected_version("css/essai-absent.css"))

    def test_on_the_server_the_address_asset_prints_is_kept_a_year(self):
        """WhiteNoise said « max-age=60 » of every file: after a minute each
        page asked again for its stylesheet and topbar.js before drawing
        anything, a round trip through the tunnel. The address `{% asset %}`
        prints names those bytes for as long as the server runs: kept a year.
        Any other `v` (a guessed one would hold today's bytes a year at
        Cloudflare) and the admin's plain addresses keep the minute."""
        from django.http import HttpResponse
        from django.test import RequestFactory

        from config.static import VersionedWhiteNoiseMiddleware

        self.collected()
        middleware = VersionedWhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
        url = "/static/css/marginmate.css"
        answer = middleware(RequestFactory().get(url, {"v": "1700000000"}))
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer["Cache-Control"], "max-age=31536000, public, immutable")
        again = middleware(RequestFactory().get(url, {"v": "1700000000"}, HTTP_IF_NONE_MATCH=answer["ETag"]))
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again["Cache-Control"], "max-age=31536000, public, immutable")
        for query in ({"v": "1700000999"}, {"v": ""}, {}):
            with self.subTest(query=query):
                self.assertEqual(middleware(RequestFactory().get(url, query))["Cache-Control"], "max-age=60, public")


class InvoiceDetailTests(TestCase):
    """The reconciliation adjustment exists precisely so an invoice's total
    matches what the supplier billed - and it was invisible on the one page
    that shows that total."""

    def setUp(self):
        from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

        supplier = make_supplier(code="UBA", name="UBA")
        self.invoice = make_invoice(supplier=supplier, invoice_number="VE-1")
        product = make_product(supplier=supplier, raw_name="BIERE 30L")
        make_invoice_line(
            invoice=self.invoice,
            product=product,
            quantity=5,
            total_ht="100.00",
            vat_rate=Decimal("0.20"),
            taxes=Decimal("3.50"),
        )

    def url(self):
        return reverse("invoices:invoice_detail", kwargs={"pk": self.invoice.pk})

    def test_the_adjustment_is_shown_and_explained(self):
        self.invoice.reconciliation_adjustment = Decimal("0.30")
        self.invoice.save(update_fields=["reconciliation_adjustment"])
        html = self.client.get(self.url()).content.decode()
        self.assertIn("0,30", html.replace(".", ","))
        self.assertIn("Régularisation", html)
        self.assertIn("droits", html)

    def test_no_adjustment_means_no_extra_rows(self):
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn("Régularisation", html)

    def test_vat_is_shown_as_a_percentage_not_a_fraction(self):
        """0.200 read as a currency amount to everyone who saw it."""
        html = self.client.get(self.url()).content.decode()
        self.assertIn("20 %", html)
        self.assertNotIn("0.200", html)

    def test_the_totals_add_up(self):
        self.invoice.reconciliation_adjustment = Decimal("0.30")
        self.invoice.save(update_fields=["reconciliation_adjustment"])
        self.assertEqual(self.invoice.lines_total_ht, Decimal("100.00"))
        self.assertEqual(self.invoice.total_ht, Decimal("100.30"))
        # 100 at 20% = 120, plus the adjustment AT 20%: it is duty, and duty
        # is part of the VAT base. Added flat, every UBA total fell five or
        # six cents short of what the bank actually debited for it.
        self.assertEqual(self.invoice.total_ttc, Decimal("120.36"))

    def test_ttc_mixes_rates_per_line_rather_than_blending_them(self):
        from tests.factories import make_invoice_line, make_product

        food = make_product(supplier=self.invoice.supplier, raw_name="COMTE")
        make_invoice_line(
            invoice=self.invoice,
            product=food,
            quantity=1,
            total_ht="100.00",
            vat_rate=Decimal("0.055"),
        )
        self.assertEqual(self.invoice.total_ttc, Decimal("120.00") + Decimal("105.50"))

    def test_taxes_and_discounts_are_visible_on_the_line(self):
        """They're why the real unit cost differs from the printed price."""
        html = self.client.get(self.url()).content.decode()
        self.assertIn("de taxes", html)

    def test_the_page_links_back_to_the_list(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn(reverse("invoices:invoice_list"), html)


class StockTakeDetailTests(TestCase):
    def setUp(self):
        from inventory.models import UnitChoices
        from tests.factories import make_stock_take, make_stock_take_line, make_stock_type

        self.take = make_stock_take()
        self.stock_type = make_stock_type(name="Vodka", unit=UnitChoices.LITRE)
        make_stock_take_line(
            stock_take=self.take,
            stock_type=self.stock_type,
            counted_quantity="4",
            unit=UnitChoices.LITRE,
            value_ht="40",
        )

    def url(self):
        return reverse("inventory:stock_take_detail", kwargs={"pk": self.take.pk})

    def test_the_total_is_a_headline_not_a_footnote(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("stat-value", html)
        self.assertIn("Valeur comptée", html)

    def test_a_shortfall_is_counted_and_explained(self):
        from inventory.models import UnitChoices
        from tests.factories import make_stock_take_line, make_stock_type

        make_stock_take_line(
            stock_take=self.take,
            stock_type=make_stock_type(name="Gin"),
            counted_quantity="9",
            unit=UnitChoices.LITRE,
            value_ht="90",
            has_shortfall=True,
            shortfall_quantity=Decimal("3"),
        )
        response = self.client.get(self.url())
        self.assertEqual(response.context["shortfall_count"], 1)
        self.assertContains(response, "au-delà")

    def test_no_shortfall_means_no_warning(self):
        response = self.client.get(self.url())
        self.assertEqual(response.context["shortfall_count"], 0)
        self.assertNotContains(response, "stat-warn")

    def test_the_page_links_to_its_variance_report(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn(reverse("inventory:stock_take_variance", kwargs={"pk": self.take.pk}), html)


class LayoutClassTests(TestCase):
    """Classes used by templates but never defined in the stylesheet don't
    error - the element just gets no layout, which is how six button groups
    ended up with none."""

    def test_every_class_a_template_uses_for_layout_is_defined(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        css = (root / "static/css/marginmate.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
        # Only the structural ones: utility and state classes are applied by
        # JavaScript or come from Django, and aren't all styled.
        # .table-scroll and .phone-cards (30/09): a table's box on a phone,
        # and the lists read as cards there - unstyled, both do nothing.
        structural = {
            "actions",
            "page-header-actions",
            "stat-row",
            "stat",
            "form-grid",
            "form-field",
            "table-wrap",
            "table-scroll",
            "phone-cards",
            "bulk-bar",
            "job-console",
            "chart",
            "explainer",
            "lead",
            "breadcrumb",
        }
        missing = sorted(name for name in structural if name not in defined)
        self.assertEqual(missing, [], f"Structural classes with no CSS rule: {missing}")


class TransferReportTableTests(TestCase):
    """The report of an import or a clear (« Données ») is a summary, not a
    list to search: no data-table, a row per section and entity, the
    rebuilt data last, in the future before a confirm and in the past
    after."""

    def render(self, preview):
        from django.template.loader import render_to_string

        from transfer.report import RunReport, SectionReport

        section = SectionReport(key="factures", label="Factures et tickets")
        section.created("documents", 2)
        section.unchanged("lignes", 5)
        for number in range(25):
            section.conflict(f"Facture n° {number} : différente dans l'archive")
        report = RunReport(mode="import", preview=preview, sections=[section], rebuilt={"mouvements de stock": 3})
        return render_to_string("transfer/_report.html", {"report": report})

    def test_the_preview_speaks_in_the_future(self):
        html = self.render(preview=True)
        self.assertIn('<table class="summary-table">', html)
        self.assertNotIn("data-table", html)
        for heading in ("Partie", "À créer", "À modifier", "À supprimer", "Inchangés"):
            self.assertIn(heading, html)
        self.assertIn("Factures et tickets › documents", html)
        self.assertIn("Factures et tickets › lignes", html)
        self.assertLess(html.index("› lignes"), html.index("Recalculé"))
        self.assertIn("mouvements de stock : 3", html)
        self.assertIn("Conflits — gardés tels quels (25)", html)
        self.assertIn("… et 5 autres", html)

    def test_the_final_report_in_the_past(self):
        html = self.render(preview=False)
        for heading in ("Créés", "Modifiés", "Supprimés", "Inchangés"):
            self.assertIn(heading, html)
        self.assertNotIn("À créer", html)


# ------------------------------------------------------------ the phone layout
#
# The UX review of 30/09, on the owner's phone (375 px): the topbar took a
# sixth of the screen on every page (« the top menu is too big, maybe do
# something that can be expanded »), « Produits & charges » printed its
# columns over one another (« columns overlapping and hard to read »), four
# pages were wider than the screen, and the page would not scroll under
# « À classer » (« I cannot scroll down when I have new products to
# classify »). Chrome measures the fixes (tests/test_phone_width_browser.py,
# accounts/tests/test_topbar_browser.py, the products page's own); the
# classes below are the fast loop's guards of the rules and the markup they
# rest on, read on the files themselves - every one of them fails silently:
# the page still renders, on a laptop exactly as before.

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A rule of marginmate.css: where it starts in the file, the condition of
#: the @media block around it (None at the top level, « @container … » in a
#: container query), its selectors and what it declares.
Rule = namedtuple("Rule", "where media selectors declarations")

#: A <table> of a template: its classes, the box nearest around it (None:
#: straight in the page) and whether datatable.js gives it a search box.
TemplateTable = namedtuple("TemplateTable", "classes box searchable")

#: The boxes a table may sit in (marginmate.css): .table-wrap and
#: .table-scroll anywhere, the stock list's own (.sub-table-wrap round an
#: article's purchases, .stock-table-wrap between two counts), the
#: timesheet's grid (« Enregistrer » sticks under it) and a month's table
#: (three columns that fit a phone, clipped rather than scrolled).
TABLE_BOXES = (
    "table-wrap",
    "table-scroll",
    "sub-table-wrap",
    "stock-table-wrap",
    "timesheet-grid-wrap",
    "month-table-wrap",
)

#: What an HTML parser opens and never closes.
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

#: What a script writing markup looks like (charts.js was caught at it).
MARKUP_WRITERS = (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML", r"document\.write")


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _blank_comments(text: str) -> str:
    """Comments as spaces of their own length: what a rule says, never what a
    comment says about it - and every rule where the file has it."""
    return re.sub(r"/\*.*?\*/", lambda comment: re.sub(r"[^\n]", " ", comment.group()), text, flags=re.DOTALL)


def _split_selectors(group: str) -> list:
    """« a, :is(b, c) d » is two selectors: a comma in brackets is the
    selector's own."""
    parts, depth, current = [], 0, ""
    for character in group:
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(character, 0)
        if character == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    parts.append(current)
    return [" ".join(part.split()) for part in parts if part.strip()]


def _declarations(body: str) -> dict:
    found = {}
    for declaration in body.split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            found[name.strip()] = " ".join(value.split())
    return found


def stylesheet_rules(css: str | None = None) -> list:
    """Every rule of `css` (marginmate.css by default), in the file's order,
    braces matched: a block of @media is read rule by rule. @keyframes are
    left out - their « from » and « to » are no selectors."""
    text = _blank_comments(css if css is not None else _source("static/css/marginmate.css"))
    found = []

    def walk(position, end, media):
        while True:
            opening = text.find("{", position, end)
            if opening < 0:
                return
            depth, index = 1, opening + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            raw = text[position:opening]
            prelude = " ".join(raw.split())
            where = position + len(raw) - len(raw.lstrip())
            if prelude.startswith("@media"):
                walk(opening + 1, index - 1, prelude[len("@media") :].strip())
            elif prelude.startswith("@container"):
                walk(opening + 1, index - 1, prelude)
            elif not prelude.startswith("@"):
                found.append(
                    Rule(where, media, _split_selectors(prelude), _declarations(text[opening + 1 : index - 1]))
                )
            position = index

    walk(0, len(text), None)
    return found


def stylesheet_section(name: str) -> tuple:
    """Where marginmate.css's « /* ----- name */ » section starts, and where
    the next one does (or the file ends)."""
    css = _source("static/css/marginmate.css")
    markers = [(match.start(), match.group(1).strip()) for match in re.finditer(r"/\* -{5,} ([^*]+?) \*/", css)]
    for index, (start, title) in enumerate(markers):
        if title == name:
            return start, markers[index + 1][0] if index + 1 < len(markers) else len(css)
    raise AssertionError(f"marginmate.css has no « {name} » section")


def template_markup(source: str) -> str:
    """A template's source as markup: Django's comments dropped and its tags
    read as a space, so what is drawn under an {% if %} counts as drawn."""
    source = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", source, flags=re.DOTALL)
    source = re.sub(r"\{#.*?#\}", "", source)
    # A tag inside an attribute (class="a{% if b %} c{% endif %}") keeps its words.
    return re.sub(r"\{%.*?%\}", " ", source)


def template_tables(source: str) -> list:
    """Every <table> of a template's source (TemplateTable), read through
    template_markup: the stock list's 7-column table sits in
    .stock-table-wrap between two counts only, and is read as cards on a
    phone (_catalogue.html)."""
    source = template_markup(source)

    class Finder(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.open, self.tables = [], []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            classes = set((attributes.get("class") or "").split())
            if tag == "table":
                boxes = [box for _, held in self.open for box in TABLE_BOXES if box in held]
                self.tables.append(
                    TemplateTable(
                        ".".join(sorted(classes)) or "(sans classe)",
                        boxes[-1] if boxes else None,
                        "data-table" in attributes and "data-table-sort-only" not in attributes,
                    )
                )
            if tag not in VOID_ELEMENTS:
                self.open.append((tag, classes))

        def handle_endtag(self, tag):
            for index in range(len(self.open) - 1, -1, -1):
                if self.open[index][0] == tag:
                    del self.open[index:]
                    return

    finder = Finder()
    finder.feed(source)
    return finder.tables


class StylesheetTestCase(SimpleTestCase):
    """The rules of marginmate.css, read once per class."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rules = stylesheet_rules()

    def naming(self, selector, media=None) -> list:
        """The rules listing `selector` - exactly, as one of their selectors -
        under the @media condition `media` (None: at the top level)."""
        return [rule for rule in self.rules if rule.media == media and selector in rule.selectors]

    def declared(self, selector, media=None) -> dict:
        """What those rules declare, the later winning: they weigh the same."""
        merged = {}
        for rule in self.naming(selector, media):
            merged.update(rule.declarations)
        return merged

    def assertDeclares(self, selector, media, expected: dict):
        where = f"« {selector} » " + (f"under @media {media}" if media else "at the top level")
        found = self.declared(selector, media)
        self.assertTrue(found, f"marginmate.css has no rule for {where}")
        for name, value in expected.items():
            self.assertEqual(found.get(name), value, f"{where}: {name}")

    def assertAfterItsBaseRule(self, selector, media, name):
        """The rule of `media` setting `name` on `selector` comes after the
        top-level one it corrects: they weigh the same, and the later wins -
        written before, it is dead."""
        base = [rule.where for rule in self.naming(selector) if name in rule.declarations]
        phone = [rule.where for rule in self.naming(selector, media) if name in rule.declarations]
        self.assertTrue(base, f"no top-level « {selector} » setting {name}")
        self.assertTrue(phone, f"no « {selector} » setting {name} under @media {media}")
        self.assertGreater(
            max(phone), max(base), f"« {selector} » {{ {name} }} under @media {media} comes before the rule it corrects"
        )


class EveryTableScrollsInItsOwnBoxTests(SimpleTestCase):
    """A <table> sits in a box that scrolls sideways, or a phone is as wide
    as its widest row. At 375 px a supplier's page was 449 px wide and a
    recipe's 505, their tables straight in a .card, and Chrome then widened
    the whole page: it zoomed out sideways and the sticky topbar drifted out
    of view (UX review, 30/09). Read on the templates themselves, so a table
    only a rare page draws - a gather's test, an inventory's « Incohérences »
    - is held to it too. Ten were bare that day."""

    def every_table(self):
        paths = sorted({*ROOT.glob("templates/**/*.html"), *ROOT.glob("*/templates/**/*.html")})
        for path in paths:
            if ".venv" in path.parts:
                continue
            for table in template_tables(path.read_text(encoding="utf-8")):
                yield path.relative_to(ROOT).as_posix(), table

    def test_every_table_is_in_a_scroll_box(self):
        bare = [f"{name}: table.{table.classes}" for name, table in self.every_table() if table.box is None]
        self.assertEqual(
            bare,
            [],
            "A table straight in the page makes a phone as wide as its widest row: put it in a "
            ".table-scroll (or a .table-wrap):\n" + "\n".join(bare),
        )

    def test_a_table_s_search_box_goes_before_its_box(self):
        """datatable.js puts a table's search box before the box the table
        scrolls in: inside it, the box scrolled away sideways with the
        columns on a phone. So every table that gets one sits in a box the
        script knows."""
        anchor = re.search(r'table\.closest\("([^"]+)"\)', _source("static/js/datatable.js"))
        self.assertIsNotNone(anchor, "datatable.js no longer says which box its search box goes before")
        known = {name.strip().lstrip(".") for name in anchor.group(1).split(",")}
        searchable = [(name, table) for name, table in self.every_table() if table.searchable]
        misplaced = [
            f"{name}: table.{table.classes} in .{table.box}" for name, table in searchable if table.box not in known
        ]
        self.assertEqual(misplaced, [], "\n".join(misplaced))
        # Not a guard of nothing: tables with a search box sit in both boxes.
        self.assertEqual(
            {table.box for _, table in searchable} & {"table-wrap", "table-scroll"}, {"table-wrap", "table-scroll"}
        )

    def test_the_guard_sees_a_bare_table(self):
        """The parser itself: a table in a .card is bare; in a .table-scroll,
        or behind an {% if %}'s box, it is not; a box closed before it holds
        nothing; a comment's table is no table."""
        boxes = lambda source: [(table.classes, table.box) for table in template_tables(source)]
        self.assertEqual(boxes('<div class="card"><table class="sub-table"></table></div>'), [("sub-table", None)])
        self.assertEqual(
            boxes('<div class="card"><div class="table-scroll"><table></table></div></div>'),
            [("(sans classe)", "table-scroll")],
        )
        self.assertEqual(
            boxes(
                '{% if a %}<div class="stock-table-wrap">{% endif %}<table class="x{% if a %} y{% endif %}"></table>'
            ),
            [("x.y", "stock-table-wrap")],
        )
        self.assertEqual(boxes('<div class="table-scroll"></div><table></table>'), [("(sans classe)", None)])
        self.assertEqual(boxes("{% comment %}<table></table>{% endcomment %}{# <table> #}"), [])
        self.assertEqual(
            boxes('<div class="table-wrap"><table><tr><td><table class="inner"></table></td></tr></table></div>'),
            [("(sans classe)", "table-wrap"), ("inner", "table-wrap")],
        )
        searchable = [
            table.searchable
            for table in template_tables(
                "<table data-table></table><table data-table data-table-sort-only></table><table></table>"
            )
        ]
        self.assertEqual(searchable, [True, False, False])


class PhoneWidthStylesheetTests(StylesheetTestCase):
    """The fast loop's guard of what tests/test_phone_width_browser.py
    measures in Chrome at 320, 375 and 430 px: nothing on a page wider than
    the phone."""

    def test_a_stacked_receipt_review_never_grows_past_the_screen(self):
        """Stacked under 900 px, a ticket's check had `1fr` - minmax(auto,
        1fr) - and its one column grew to its known prices' table: 507 px on
        a 375 px phone (30/09). The same for a PDF beside its lines."""
        for selector in (".receipt-review", ".receipt-review:has(.document-pdf)"):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, "(max-width: 900px)", {"grid-template-columns": "minmax(0, 1fr)"})
        bare = [
            rule
            for rule in self.rules
            if any(".receipt-review" in selector for selector in rule.selectors)
            and rule.declarations.get("grid-template-columns") == "1fr"
        ]
        self.assertEqual(bare, [])

    def test_a_stacked_receipt_photo_is_part_of_the_page(self):
        """Above the lines, the photo is no sticky box as tall as the screen
        scrolling on its own - a second page inside the page, where a finger
        on the receipt scrolled the photo and not the lines (« there are
        scroll issues with some panels », 30/09). And a PDF's frame leaves
        the page reachable around it: 60 % of the screen, in svh."""
        self.assertDeclares(
            ".receipt-photo", "(max-width: 900px)", {"position": "static", "max-height": "none", "overflow": "visible"}
        )
        for name in ("position", "max-height"):
            with self.subTest(declaration=name):
                self.assertAfterItsBaseRule(".receipt-photo", "(max-width: 900px)", name)
        self.assertRegex(self.declared(".document-frame", "(max-width: 900px)").get("height", ""), r"^\d+svh$")

    def test_words_fields_and_buttons_give_way_on_a_phone(self):
        """« phone widths »: a word longer than its line breaks there, a
        button's words wrap (« + Nouvelle source pour » a supplier's name
        made his page 449 px wide) - but not in a table's cell, where the
        table scrolls in its own box -, a field is never wider than its line
        outside a cell, and the stock list's period no longer holds 16rem
        and an inventory's note (551 px at 375)."""
        phone = "(max-width: 860px)"
        self.assertDeclares("body", phone, {"overflow-wrap": "break-word"})
        self.assertDeclares(".btn", phone, {"white-space": "normal"})
        self.assertAfterItsBaseRule(".btn", phone, "white-space")
        self.assertDeclares(":is(td, th) .btn", phone, {"white-space": "nowrap"})
        fields = [
            rule
            for rule in self.rules
            if rule.media == phone
            and rule.declarations.get("max-width") == "100%"
            and any(
                ":not(:is(td, th) *)" in selector and all(name in selector for name in ("input", "select", "textarea"))
                for selector in rule.selectors
            )
        ]
        self.assertTrue(fields, "no rule keeping a field (outside a table's cell) to its line's width")
        self.assertDeclares(".period-picker select", phone, {"min-width": "0"})
        self.assertAfterItsBaseRule(".period-picker select", phone, "min-width")

    def test_a_grid_of_fields_never_asks_more_than_its_card(self):
        """A form's grid and the import's two choices ask each column for
        15rem / 240 px at least: on a folding phone's 280 px cover screen a
        card is narrower, and one column overflowed it. min(…, 100%) keeps
        one column there (30/09) - at 320 px and up nothing shows it, so no
        page measured in Chrome would."""
        for selector in (".form-grid", ".upload-choices"):
            with self.subTest(selector=selector):
                columns = self.declared(selector).get("grid-template-columns", "")
                self.assertRegex(columns, r"minmax\(min\([^,]+, 100%\), 1fr\)")

    def test_a_file_field_in_its_choice_is_held_never_sized(self):
        """A file field kept to its choice's card by `max-width`, never given
        a `width`: `.upload-choice input[type=file]` weighs more than
        .visually-hidden, and its `width: 100%` drew Consignes' camera input
        - hidden under its label - as wide as the page (review of 30/09;
        returnables/tests/test_phone_browser.py measures the page)."""
        self.assertDeclares(".visually-hidden", None, {"width": "1px"})
        declared = self.declared('.upload-choice input[type="file"]')
        self.assertEqual(declared.get("max-width"), "100%")
        self.assertNotIn("width", declared)

    def test_nothing_of_it_reaches_a_laptop(self):
        """Every rule of « phone widths » sits in a phone's block (860 px, or
        600 for the cards' padding and the fields sized for a laptop): one
        at the top level would wrap a laptop's buttons and break its words
        too, with every page still drawn - the review of 30/09 changed
        nothing above 860 px."""
        start, end = stylesheet_section("phone widths")
        section = [rule for rule in self.rules if start <= rule.where < end]
        self.assertGreater(len(section), 5)
        outside = [
            f"{', '.join(rule.selectors)} (@media {rule.media})"
            for rule in section
            if rule.media not in ("(max-width: 860px)", "(max-width: 600px)")
        ]
        self.assertEqual(outside, [], "\n".join(outside))

    def test_the_cards_are_drawn_on_a_phone_only(self):
        """Every rule naming .phone-cards sits in a « max-width: 860px »
        block: one outside it would redraw a laptop's tables. There a card
        draws its header's words over each figure (data-label), its header
        row gone, each row a grid."""
        cards = [rule for rule in self.rules if any("phone-cards" in selector for selector in rule.selectors)]
        self.assertGreater(len(cards), 10)
        outside = [
            f"{', '.join(rule.selectors)} (@media {rule.media})" for rule in cards if rule.media != "(max-width: 860px)"
        ]
        self.assertEqual(outside, [], "\n".join(outside))
        self.assertDeclares("table.phone-cards > thead", "(max-width: 860px)", {"display": "none"})
        self.assertDeclares("table.phone-cards > tbody > tr", "(max-width: 860px)", {"display": "grid"})
        labels = [
            rule for rule in cards if any(selector.endswith("td[data-label]::before") for selector in rule.selectors)
        ]
        self.assertTrue(labels, "no rule drawing a card's labels")
        self.assertEqual(labels[-1].declarations.get("content"), "attr(data-label)")


class PhoneCardsLabelTests(TestCase):
    """Under 860 px a list marked .phone-cards is read as cards, its header
    row not drawn: each figure says what it is from its cell's data-label,
    which is the column's header word for word (marginmate.css, « phone
    cards »). A label typed otherwise - or forgotten - is a figure on a
    phone nobody can name: « 12 » under no word. Every cell but the card's
    title (its first) and the cells a header names nothing for, on every
    kind of row these lists draw. Achats' documents, « Documents à
    corriger », an import's files, the recipes (30/09). Data invented."""

    class Cards(HTMLParser):
        """The tables a page marks .phone-cards: their attributes, their
        header's words and, row by row, each cell's classes, colspan, label
        and column."""

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tables, self.open, self.cell = [], [], None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "table":
                self.open.append({"attributes": attributes, "head": [], "rows": [], "part": None})
                if "phone-cards" in (attributes.get("class") or "").split():
                    self.tables.append(self.open[-1])
            elif self.open and tag in ("thead", "tbody", "tfoot"):
                self.open[-1]["part"] = tag
            elif self.open and tag == "tr" and self.open[-1]["part"] == "tbody":
                self.open[-1]["rows"].append([])
            elif self.open and tag in ("th", "td"):
                table = self.open[-1]
                self.cell = {
                    "classes": (attributes.get("class") or "").split(),
                    "span": int(attributes.get("colspan") or 1),
                    "label": attributes.get("data-label"),
                    "text": "",
                }
                if table["part"] == "thead":
                    table["head"].append(self.cell)
                elif table["part"] == "tbody" and table["rows"]:
                    table["rows"][-1].append(self.cell)

        def handle_endtag(self, tag):
            if tag == "table" and self.open:
                self.open.pop()
            elif tag in ("th", "td"):
                self.cell = None

        def handle_data(self, data):
            if self.cell is not None:
                self.cell["text"] += data

    def cards(self, url, attribute, value) -> dict:
        parser = self.Cards()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        parser.feed(response.content.decode())
        found = [table for table in parser.tables if table["attributes"].get(attribute) == value]
        self.assertEqual(len(found), 1, f"{url}: no table.phone-cards with {attribute}={value!r}")
        return found[0]

    def assertLabelled(self, table, rows):
        """Each cell under a header with words, the title aside, carries the
        header's words; a cell spanning columns (a message) is the row's own."""
        headers = []
        for cell in table["head"]:
            headers += [" ".join(cell["text"].split())] * cell["span"]
        self.assertEqual(len(table["rows"]), rows)
        for number, row in enumerate(table["rows"]):
            column, title = 0, None
            for cell in row:
                header = headers[column] if column < len(headers) else ""
                if title is None and "select-col" not in cell["classes"]:
                    title = cell
                    self.assertIsNone(cell["label"], f"row {number}: the card's title needs no label")
                elif cell["span"] == 1 and header:
                    self.assertEqual(cell["label"], header, f"row {number}, column « {header} »")
                elif cell["label"] is not None:
                    self.assertEqual(cell["label"], header, f"row {number}, column {column}")
                column += cell["span"]

    def test_purchases_documents(self):
        from invoices.models import Invoice

        cellar = make_supplier(name="Cave Exemple")
        make_invoice(supplier=cellar, invoice_number="F-1")
        make_invoice(
            supplier=cellar, invoice_number="", parse_checks=[{"label": "Total", "passed": True, "detail": ""}]
        )
        make_invoice(supplier=cellar, invoice_number="F-3", status=Invoice.Status.NEEDS_REVIEW)
        table = self.cards(reverse("invoices:invoice_list"), "data-table-label", "documents")
        self.assertLabelled(table, 3)

    def test_documents_to_fix(self):
        from invoices.models import Invoice

        make_invoice(
            supplier=make_supplier(name="Cave Exemple"),
            invoice_number="F-9",
            status=Invoice.Status.ERROR,
            error_message="Lecture impossible (inventé).",
        )
        table = self.cards(reverse("invoices:receipt_queue"), "data-table-label", "documents à corriger")
        self.assertLabelled(table, 1)

    def test_an_import_s_files_on_every_kind_of_row(self):
        from invoices.models import ReceiptBatch

        shop = make_supplier(name="Épicerie Exemple")
        ticket = make_invoice(supplier=shop, parse_checks=[{"label": "Total", "passed": True, "detail": ""}])
        gone = make_invoice(supplier=shop)
        gone_pk = gone.pk
        gone.delete()
        batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {
                    "name": "IMG_20260312_101112.jpg",
                    "status": "ok",
                    "invoice_id": ticket.pk,
                    "receipt": True,
                    "shop": "Épicerie Exemple",
                },
                {
                    "name": "IMG_20260312_101113.jpg",
                    "status": "ok",
                    "invoice_id": gone_pk,
                    "receipt": True,
                    "shop": "Épicerie Exemple",
                },
                {"name": "IMG_20260312_101114.jpg", "status": "duplicate", "message": "Déjà importé le 12/03/2026"},
                {
                    "name": "IMG_20260312_101115.jpg",
                    "status": "unrecognised",
                    "header": "EPICERIE INCONNUE EXEMPLE",
                    "read_date": "12/03/2026",
                    "read_total": "4,20",
                },
                {"name": "IMG_20260312_101116.jpg", "status": "pending"},
            ],
        )
        table = self.cards(reverse("invoices:receipt_batch", args=[batch.pk]), "class", "phone-cards")
        self.assertLabelled(table, 5)

    def test_the_recipes_and_their_article_s_column(self):
        rum = make_priced_stock_type(name="Rhum exemple", unit_cost_ht="20", quantity="1")
        make_ingredient(make_recipe(name="Punch exemple", selling_price_ttc="7.50"), stock_type=rum, quantity="0.04")
        make_recipe(name="Sirop maison exemple", selling_price_ttc=None)
        self.assertLabelled(self.cards(reverse("recipes:recipe_list"), "data-table-label", "recettes"), 2)
        chosen = self.cards(f"{reverse('recipes:recipe_list')}?article={rum.pk}", "data-table-label", "recettes")
        self.assertIn("Utilisé", [cell["text"].strip() for cell in chosen["head"]])
        self.assertLabelled(chosen, 1)

    def test_the_gaps_to_fill_with_and_without_a_list(self):
        """« Combler les écarts »: the gaps on every kind of row (to fill,
        never counted, sold past what it had), the sales to ring up and the
        amounts typed before them. The gaps grow « Proposé » and « Comblé »
        once the list holds an amount: both states, each cell under its own
        header's words."""
        take = make_gaps_to_fill()
        page = f"{reverse('inventory:stock_gap_filler')}?depuis={take.pk}"

        def headers(table):
            return [" ".join(cell["text"].split()) for cell in table["head"]]

        gaps = self.cards(page, "data-table-label", "écarts")
        self.assertNotIn("Proposé", headers(gaps))
        self.assertNotIn("Comblé", headers(gaps))
        self.assertEqual(headers(gaps)[-2:], ["Valeur (HT)", ""])
        self.assertLabelled(gaps, 3)
        self.assertExcludeCellLast(gaps)

        # 70 € is one bottle at 36 € and one at 34 €; then 36 €, the red
        # alone; then 34 €, the rosé alone.
        for amount in ("70", "36", "34"):
            added = self.client.post(reverse("inventory:stock_gap_filler_add"), {"depuis": take.pk, "montant": amount})
            self.assertEqual(added.status_code, 302)
        gaps = self.cards(page, "data-table-label", "écarts")
        # The list's two columns come before the row's « Exclure », whose
        # header says nothing.
        self.assertEqual(headers(gaps)[-3:], ["Proposé", "Comblé", ""])
        self.assertLabelled(gaps, 3)
        self.assertExcludeCellLast(gaps)
        # The last amount, one recipe; the two typed before it, one row each.
        self.assertLabelled(self.cards(page, "data-table-label", "recettes à encaisser"), 1)
        earlier = self.cards(page, "data-table-label", "montants déjà saisis")
        self.assertEqual(headers(earlier), ["Saisi", "Montant (TTC)", "Proposé (TTC)", "Recettes"])
        self.assertLabelled(earlier, 2)

    def test_the_treasury_s_balances_and_adjustments(self):
        """« Trésorerie »'s « Soldes saisis » on every kind of row (the
        first, one that agrees, one to resolve, one waiting for a statement)
        and its « Ajustements », counted and counting nowhere. Invented."""
        from bank.models import BankTransaction, TreasuryAdjustment, TreasuryCheckpoint

        for number, (day, amount) in enumerate(((date(2026, 9, 5), "100.00"), (date(2026, 9, 12), "-30.00"))):
            BankTransaction.objects.create(
                operation_date=day,
                label=f"VIR EXEMPLE {number}",
                amount=Decimal(amount),
                fingerprint=f"ui-treso-{number}",
            )
        for day, balance in ((1, "1000.00"), (6, "1100.00"), (12, "1000.00"), (20, "990.00")):
            TreasuryCheckpoint.objects.create(date=date(2026, 9, day), balance=Decimal(balance))
        TreasuryAdjustment.objects.create(date=date(2026, 9, 12), amount=Decimal("-10.00"), reason="Frais exemple")
        TreasuryAdjustment.objects.create(date=date(2026, 8, 1), amount=Decimal("5.00"))
        page = f"{reverse('bank:treasury')}?tout=1"
        points = self.cards(page, "data-table-label", "soldes saisis")
        self.assertLabelled(points, 4)
        said = [" ".join(row[2]["text"].split()) for row in points["rows"]]
        self.assertEqual(said, ["relevé à importer", "60.00 € de moins : à résoudre", "concorde", "—"])
        adjustments = self.cards(page, "data-table-label", "ajustements")
        self.assertLabelled(adjustments, 2)
        self.assertEqual([" ".join(row[3]["text"].split()) for row in adjustments["rows"]], ["compte", "ne compte pas"])

    def assertExcludeCellLast(self, table):
        """Every gaps row ends on its « Exclure »: a .row-actions cell, which
        a card draws across at its foot, under no label - its header has no
        words, and « EXCLURE » printed over a button saying « Exclure » is
        noise."""
        for number, row in enumerate(table["rows"]):
            last = row[-1]
            self.assertEqual(last["classes"], ["row-actions"], f"row {number}")
            self.assertIsNone(last["label"], f"row {number}")
            self.assertEqual(last["text"].strip(), "Exclure", f"row {number}")
            # One action cell a row: the figures keep their labels.
            self.assertEqual(sum("row-actions" in cell["classes"] for cell in row), 1, f"row {number}")

    def assertActionCellLast(self, table, words):
        """Every row ends on its one action (« Pas ici », « Ne plus
        proposer »…): a .row-actions cell, drawn across at the card's foot,
        under no label."""
        for number, row in enumerate(table["rows"]):
            last = row[-1]
            self.assertEqual(last["classes"], ["row-actions"], f"row {number}")
            self.assertIsNone(last["label"], f"row {number}")
            self.assertEqual(last["text"].strip(), words, f"row {number}")
            self.assertEqual(sum("row-actions" in cell["classes"] for cell in row), 1, f"row {number}")

    def assertReasonAcross(self, table):
        """A sentence is no figure: the « Pourquoi » cell runs across the
        card (.phone-card-wide), never squeezed into a third of it beside
        the quantity and the chance."""
        headers = [" ".join(cell["text"].split()) for cell in table["head"]]
        self.assertIn("Pourquoi", headers)
        column = headers.index("Pourquoi")
        for number, row in enumerate(table["rows"]):
            self.assertEqual(row[column]["label"], "Pourquoi", f"row {number}")
            self.assertIn("phone-card-wide", row[column]["classes"], f"row {number}")

    def test_the_shopping_list_its_folds_and_the_rhythm(self):
        """« Prévoir les courses »: the list and every fold drawn as a table,
        each figure under its own header's words, the line's action last -
        the three sections with a quantity, under « Produit » and « À
        acheter »; « Rythme d'achat », for every store and for one (its
        « Habitude ici » column then). Invented data (make_shopping_history)."""
        made = make_shopping_history()
        page = reverse("inventory:shopping_list")
        for label, rows, action, quantity in (
            ("à acheter", 2, "Pas ici", True),
            ("peut-être", 1, "Pas ici", True),
            ("nouveaux ici", 1, "Pas ici", True),
            ("plus acheté", 1, "Ne plus proposer", False),
            ("acheté ailleurs", 1, None, False),
            ("à acheter ailleurs", 1, "Ne plus proposer", False),
            ("les plus achetés ici", 7, None, False),
        ):
            with self.subTest(table=label):
                table = self.cards(page, "data-table-label", label)
                self.assertLabelled(table, rows)
                if action:
                    self.assertActionCellLast(table, action)
                if label != "les plus achetés ici":
                    self.assertReasonAcross(table)
                headers = [" ".join(cell["text"].split()) for cell in table["head"]]
                self.assertNotIn("Quantité", headers)
                if quantity:
                    self.assertEqual(headers[:3], ["Article", "Produit", "À acheter"])
                    # « Liste » (« Ajouter ») runs across the card, before the action.
                    self.assertEqual(headers[-2:], ["Liste", ""])
                    for number, row in enumerate(table["rows"]):
                        self.assertEqual([cell["label"] for cell in row[1:3]], ["Produit", "À acheter"], number)
                        self.assertIn("num", row[2]["classes"], number)
                        self.assertEqual(row[-2]["label"], "Liste", number)
                        self.assertIn("phone-card-wide", row[-2]["classes"], number)
                else:
                    self.assertNotIn("Produit", headers)
                    self.assertNotIn("À acheter", headers)
                    self.assertNotIn("Liste", headers)
        rhythm = reverse("inventory:shopping_rhythm")
        for query, rows in (("", 9), (f"?fournisseur={made.wholesaler.pk}", 7)):
            with self.subTest(rhythm=query):
                table = self.cards(f"{rhythm}{query}", "data-table-label", "rythme d'achat")
                self.assertLabelled(table, rows)
                self.assertActionCellLast(table, "Ne jamais proposer")
                headers = [" ".join(cell["text"].split()) for cell in table["head"]]
                self.assertEqual("Habitude ici" in headers, bool(query))

    def test_the_shopping_lists(self):
        """« Listes de courses »: the lists open and finished, a store's list
        - each figure under its header's words, « Modifier » and « Retirer »
        last, across the card -, and a finished list. An item counted in
        bottles says so under « Quantité ». Invented data
        (make_shopping_lists)."""
        made = make_shopping_history()
        lists = make_shopping_lists(made)
        add_bottles_of_gin(lists.open)
        index = reverse("inventory:shopping_lists")
        page = reverse("inventory:shopping_list_page")
        articles = self.cards(f"{page}?fournisseur={made.wholesaler.pk}", "data-table-label", "articles")
        (gin,) = [row for row in articles["rows"] if row[0]["text"].strip() == "Gin exemple"]
        self.assertEqual(
            [" ".join(cell["text"].split()) for cell in gin if cell["label"] == "Quantité"], ["2 bouteilles de 70 cl"]
        )
        for url, label, rows, action in (
            (index, "listes en cours", 1, ["Faire", "les", "courses"]),
            (index, "listes terminées", 1, None),
            (f"{page}?fournisseur={made.wholesaler.pk}", "articles", 4, ["Modifier", "Retirer"]),
            (f"{page}?liste={lists.finished.pk}", "courses terminées", 2, None),
        ):
            with self.subTest(table=label):
                table = self.cards(url, "data-table-label", label)
                self.assertLabelled(table, rows)
                for number, row in enumerate(table["rows"]):
                    actions = [cell for cell in row if "row-actions" in cell["classes"]]
                    if action is None:
                        self.assertEqual(actions, [], number)
                        continue
                    self.assertEqual(row[-1]["classes"], ["row-actions"], number)
                    self.assertIsNone(row[-1]["label"], number)
                    self.assertEqual(row[-1]["text"].split(), action, number)
                    self.assertEqual(len(actions), 1, number)

    def test_the_gaps_with_something_excluded(self):
        """An article left out of the gaps - alone, or with its category -
        leaves the table: what remains is still one card a row, each figure
        under its words. The fixture's red is left out alone, the amber's
        category with it (both invented)."""
        take = make_gaps_to_fill()
        page = f"{reverse('inventory:stock_gap_filler')}?depuis={take.pk}"
        StockType.objects.filter(name="Ambrée exemple").update(category="Bières exemple")
        red = StockType.objects.get(name="Rouge exemple")
        for data in ({"article": red.pk}, {"categorie": "Bières exemple"}):
            excluded = self.client.post(reverse("inventory:stock_gap_filler_exclude"), {"depuis": take.pk, **data})
            self.assertEqual(excluded.status_code, 302)
        gaps = self.cards(page, "data-table-label", "écarts")
        self.assertLabelled(gaps, 1)
        self.assertExcludeCellLast(gaps)
        self.assertIn("Rosé exemple", gaps["rows"][0][0]["text"])


class GapExclusionListTests(TestCase):
    """« Exclus des écarts » on « Combler les écarts » (#exclusions): a list,
    not a table - nothing in it to search or sort - each line its words and
    its own « Réinclure », a button of the page's kind (the touch rules size
    `main .btn` to a thumb), and the category picked like any other form
    of the page. Data invented."""

    def setUp(self):
        self.take = make_gaps_to_fill()
        self.page = f"{reverse('inventory:stock_gap_filler')}?depuis={self.take.pk}"
        StockType.objects.filter(name="Ambrée exemple").update(category="Bières exemple")

    def fold(self) -> str:
        html = self.client.get(self.page).content.decode()
        found = re.search(r'<details class="explainer" id="exclusions"[^>]*>.*?</details>', html, flags=re.DOTALL)
        self.assertIsNotNone(found)
        return found.group(0) if found else ""

    def test_each_line_its_words_and_its_button(self):
        red = StockType.objects.get(name="Rouge exemple")
        for data in ({"article": red.pk}, {"categorie": "Bières exemple"}):
            excluded = self.client.post(reverse("inventory:stock_gap_filler_exclude"), {"depuis": self.take.pk, **data})
            self.assertEqual(excluded.status_code, 302)
        fold = self.fold()
        self.assertNotIn("<table", fold)
        lists = re.findall(r'<ul class="exclusion-list">(.*?)</ul>', fold, flags=re.DOTALL)
        self.assertEqual(len(lists), 1)
        items = re.findall(r"<li>(.*?)</li>", lists[0], flags=re.DOTALL)
        self.assertEqual(len(items), 2)
        include = reverse("inventory:stock_gap_filler_include")
        for item in items:
            with self.subTest(item=item[:40]):
                forms = re.findall(r"<form\b[^>]*>.*?</form>", item, flags=re.DOTALL)
                self.assertEqual(len(forms), 1)
                self.assertIn(f'<form method="post" action="{include}">', forms[0])
                self.assertIn('<button class="btn btn-small btn-secondary" type="submit">Réinclure</button>', forms[0])
                # Its words come first, the button after them on the line.
                self.assertTrue(item.lstrip().startswith(("Catégorie", "Rouge exemple")), item)

    def test_the_category_is_picked_like_any_other_form(self):
        form = re.search(r'<form method="post" action="[^"]*" class="inline-form">.*?</form>', self.fold(), re.DOTALL)
        self.assertIsNotNone(form)
        markup = form.group(0) if form else ""
        self.assertIn('<label class="inline-label">Catégorie', markup)
        self.assertIn('<select name="categorie">', markup)
        self.assertIn('<button class="btn btn-secondary" type="submit">Exclure la catégorie</button>', markup)

    def test_nothing_excluded_is_said_and_folded(self):
        fold = self.fold()
        self.assertTrue(fold.startswith('<details class="explainer" id="exclusions">'), fold[:80])
        self.assertNotIn("exclusion-list", fold)
        self.assertIn("Rien d'exclu : tous les articles comptent.", unescape(fold))

    def test_a_message_said_in_place_is_drawn_like_any_other(self):
        """The messages said under « Écarts » and in the fold are the page's
        own `ul.messages`, each `message message-<level>` - the level alone,
        never the tag that placed it: base.html's `message.tags` would draw
        « message-ecarts success », a class no rule colours."""
        exclude = reverse("inventory:stock_gap_filler_exclude")
        red = StockType.objects.get(name="Rouge exemple")
        for data in ({"article": red.pk}, {"categorie": "Bières exemple"}, {"categorie": "Inconnue exemple"}):
            self.assertEqual(self.client.post(exclude, {"depuis": self.take.pk, **data}).status_code, 302)
        html = self.client.get(self.page).content.decode()
        # One list right under the gaps' heading, one first in the fold, open
        # for it - none at the top.
        lists = re.findall(r'<ul class="messages">(.*?)</ul>', html, flags=re.DOTALL)
        self.assertEqual(len(lists), 2)
        self.assertRegex(html, r'<h2 id="ecarts">Écarts</h2>\s*<ul class="messages">')
        self.assertRegex(html, r'id="exclusions" open>\s*<summary>[^<]*</summary>\s*<ul class="messages">')
        classes = [re.findall(r'<li class="([^"]*)">', found) for found in lists]
        self.assertEqual(classes, [["message message-success"], ["message message-success", "message message-error"]])


class ShoppingExclusionListTests(TestCase):
    """« Exclusions » on « Prévoir les courses » (#exclusions), like « Exclus
    des écarts »: a list, not a table - each line its words and its own
    « Réinclure », a button of the page's kind -, the category picked like
    any other form of the page, and the messages said there drawn like any
    other. Data invented (make_shopping_history)."""

    def setUp(self):
        self.made = make_shopping_history()
        self.page = f"{reverse('inventory:shopping_list')}?fournisseur={self.made.wholesaler.pk}"

    def fold(self) -> str:
        html = self.client.get(self.page).content.decode()
        found = re.search(r'<details class="explainer" id="exclusions"[^>]*>.*?</details>', html, flags=re.DOTALL)
        self.assertIsNotNone(found)
        return found.group(0) if found else ""

    def test_each_line_its_words_and_its_button(self):
        ShoppingExclusion.objects.create(category="Consignes exemple")
        ShoppingExclusion.objects.create(stock_type=self.made.rum)
        ShoppingExclusion.objects.create(stock_type=self.made.syrup, supplier=self.made.grocer)
        fold = self.fold()
        self.assertNotIn("<table", fold)
        lists = re.findall(r'<ul class="exclusion-list">(.*?)</ul>', fold, flags=re.DOTALL)
        self.assertEqual(len(lists), 1)
        items = re.findall(r"<li>(.*?)</li>", lists[0], flags=re.DOTALL)
        self.assertEqual(len(items), 3)
        include = reverse("inventory:shopping_include")
        for item in items:
            with self.subTest(item=item[:40]):
                forms = re.findall(r"<form\b[^>]*>.*?</form>", item, flags=re.DOTALL)
                self.assertEqual(len(forms), 1)
                self.assertIn(f'<form method="post" action="{include}">', forms[0])
                self.assertIn('<button class="btn btn-small btn-secondary" type="submit">Réinclure</button>', forms[0])
                # Its words come first, the button after them on the line.
                self.assertTrue(item.lstrip().startswith(("Catégorie", "Rhum exemple", "Sirop exemple")), item)

    def test_the_category_is_picked_like_any_other_form(self):
        form = re.search(
            r'<form method="post" action="[^"]*" class="inline-form"[^>]*>.*?</form>', self.fold(), flags=re.DOTALL
        )
        self.assertIsNotNone(form)
        markup = form.group(0) if form else ""
        self.assertIn('<label class="inline-label">Ne jamais proposer la catégorie', markup)
        self.assertIn('<select name="categorie">', markup)
        self.assertIn('<button class="btn btn-secondary" type="submit">Exclure</button>', markup)

    def test_nothing_excluded_is_said_and_folded(self):
        fold = self.fold()
        self.assertTrue(fold.startswith('<details class="explainer" id="exclusions">'), fold[:80])
        self.assertNotIn("exclusion-list", fold)
        self.assertIn("Rien d'exclu : tout article déjà acheté peut être proposé.", unescape(fold))

    def test_a_message_said_in_place_is_drawn_like_any_other(self):
        """Said above « À acheter » and first in the fold, each `message
        message-<level>` - the level alone, never the tag that placed it -,
        none at the top."""
        exclude = reverse("inventory:shopping_exclude")
        store = self.made.wholesaler.pk
        for data in (
            {"article": self.made.syrup.pk, "chez": store, "retour": "liste"},
            {"categorie": "Consignes exemple"},
            {"categorie": "Inconnue exemple"},
        ):
            self.assertEqual(self.client.post(exclude, {"fournisseur": store, **data}).status_code, 302)
        html = self.client.get(self.page).content.decode()
        lists = re.findall(r'<ul class="messages">(.*?)</ul>', html, flags=re.DOTALL)
        self.assertEqual(len(lists), 2)
        self.assertRegex(html, r'<h2 id="a-acheter">À acheter</h2>\s*<ul class="messages">')
        self.assertRegex(html, r'id="exclusions" open>\s*<summary>[^<]*</summary>\s*<ul class="messages">')
        classes = [re.findall(r'<li class="([^"]*)">', found) for found in lists]
        self.assertEqual(classes, [["message message-success"], ["message message-success", "message message-error"]])


class ShoppingStylesheetTests(StylesheetTestCase):
    def test_a_third_header_button_takes_the_row_on_a_phone(self):
        """« Produits & charges »' header on a phone is a grid of two: with
        « Prévoir les courses » it may hold three, and the last of an odd
        count takes the row across rather than half of it beside nothing."""
        self.assertDeclares(
            ".page-header .actions-compact > :last-child:nth-child(odd)",
            "(max-width: 860px)",
            {"grid-column": "1 / -1"},
        )

    def test_the_counts_typed_are_narrow(self):
        self.assertDeclares('.shopping-form input[type="number"]', None, {"width": "5rem"})

    def test_the_days_label_wraps_on_a_phone(self):
        """« Ces achats doivent tenir jusqu'au passage suivant, dans [7]
        jours » is a sentence: on a phone it wraps around its field rather
        than run off the screen. `.filter-row label` keeps every other
        filter's label on one line, and outweighs `.inline-label`'s own
        phone rule: this one names both."""
        self.assertDeclares(".filter-row.shopping-form label", "(max-width: 600px)", {"white-space": "normal"})


class ShoppingListStylesheetTests(StylesheetTestCase):
    """« Listes de courses »' tick page, read in a store with a phone in one
    hand (marginmate.css, after « Prévoir les courses »' rules)."""

    def test_each_item_is_a_button_a_thumb_tall(self):
        self.assertDeclares(".shopping-tick-button", None, {"min-height": "56px", "width": "100%", "cursor": "pointer"})
        self.assertGreaterEqual(int(self.declared(".shopping-tick-button")["min-height"].removesuffix("px")), 44)

    def test_a_ticked_item_reads_bought(self):
        self.assertDeclares(".shopping-tick.is-ticked .shopping-tick-name", None, {"text-decoration": "line-through"})
        self.assertDeclares(".shopping-tick.is-ticked .shopping-tick-box", None, {"background": "var(--green)"})

    def test_finishing_stays_at_the_foot_of_the_screen(self):
        self.assertDeclares(".shopping-finish", None, {"position": "sticky", "bottom": "0"})

    def test_the_tick_page_is_phone_first_at_every_width(self):
        """Its fields at 16 px (iOS zooms the page in under it) and its
        buttons 44 px tall, with or without a touch screen - as Consignes'."""
        self.assertDeclares(".shopping-run-page :is(input, select, textarea)", None, {"font-size": "1rem"})
        self.assertDeclares(".shopping-run-page .btn", None, {"min-height": "44px"})

    def test_no_rule_of_the_lists_unsets_everything(self):
        """`all: unset` would take the app's focus ring off the tick buttons
        (FocusRingTests)."""
        rules = [rule for rule in self.rules if any("shopping-" in selector for selector in rule.selectors)]
        self.assertGreater(len(rules), 10)
        self.assertEqual([rule.selectors for rule in rules if "all" in rule.declarations], [])


class EntryUnitsScriptTests(SimpleTestCase):
    """static/js/entry_units.js fills a unit select from the names a page
    ships - products read off suppliers' documents among them -, on the
    stock take's rows and on the shopping list's add form: nodes and text
    only, nothing kept in the browser, and both pages load it (its logic:
    inventory/tests/test_entries.py)."""

    SCRIPT = "static/js/entry_units.js"

    def test_it_writes_no_markup(self):
        source = _source(self.SCRIPT)
        for pattern in MARKUP_WRITERS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))

    def test_it_keeps_nothing_in_the_browser(self):
        self.assertIsNone(
            re.search(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b", _source(self.SCRIPT))
        )

    def test_both_pages_load_it(self):
        for template in (
            "inventory/templates/inventory/stock_take_form.html",
            "inventory/templates/inventory/shopping_list_page.html",
        ):
            with self.subTest(template=template):
                self.assertIn(
                    "<script src=\"{% asset 'js/entry_units.js' %}\" defer></script>",
                    template_markup_with_tags(template),
                )


def template_markup_with_tags(relative: str) -> str:
    """A template's source, its comments taken out (what a comment says
    about a script is not that script)."""
    return re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", _source(relative), flags=re.DOTALL)


class GapExclusionListStylesheetTests(StylesheetTestCase):
    def test_each_button_stays_on_the_line_it_takes_back(self):
        """A form is a block: without this, each « Réinclure » would fall on
        a line of its own under the words it takes back, and a list of five
        would read as ten lines, every other one a button."""
        self.assertDeclares(".exclusion-list form", None, {"display": "inline"})

    def test_a_list_of_messages_said_in_place_is_unbulleted(self):
        """Under « Écarts » and in the fold, as at the top: no bullet in
        front of a message, nor the list's indent."""
        self.assertDeclares(".messages", None, {"list-style": "none", "padding": "0"})
        for level in ("success", "warning", "error"):
            with self.subTest(level=level):
                self.assertTrue(self.declared(f".message-{level}").get("background"))


class ReviewPanelStylesheetTests(StylesheetTestCase):
    """« À classer » on Produits & charges. Under 1280 px it sits ABOVE the
    list, and kept the sticky side panel's `overflow-y: auto` and
    `overscroll-behavior: contain` on a box with nothing left to scroll:
    Chrome takes that for a wall, and a wheel or a finger on its cards moved
    nothing - on a phone the cards are the whole screen (the owner, 30/09:
    « I cannot scroll down when I have new products to classify »). Chrome
    measures it (inventory's phone tests); here, the rules."""

    PANEL_ABOVE = "(max-width: 1279px)"

    def test_above_the_list_the_panel_is_no_scroll_box(self):
        self.assertDeclares(
            ".review-panel",
            self.PANEL_ABOVE,
            {
                "position": "static",
                "max-height": "none",
                "overflow": "visible",
                "overscroll-behavior": "auto",
            },
        )

    def test_beside_the_list_it_is_still_the_box_that_scrolls_on_its_own(self):
        self.assertDeclares(
            ".review-panel", None, {"position": "sticky", "overflow-y": "auto", "overscroll-behavior": "contain"}
        )

    def test_above_the_list_it_draws_its_first_product_until_unfolded(self):
        """Every card came before the first article - screens of them. The
        next product takes the first one's place as each is classified;
        « Voir les N autres produits » is a box and its label, no script."""
        self.assertDeclares(
            ".review-panel:not(:has(.review-unfold-box:checked)) .review-card ~ .review-card",
            self.PANEL_ABOVE,
            {"display": "none"},
        )
        self.assertDeclares(
            ".review-panel:has(.review-unfold-box) .review-unfold", self.PANEL_ABOVE, {"display": "flex"}
        )
        self.assertDeclares(
            ".review-panel:has(.review-unfold-box:checked) .review-unfold-more", self.PANEL_ABOVE, {"display": "none"}
        )
        self.assertDeclares(
            ".review-panel:not(:has(.review-unfold-box:checked)) .review-unfold-less",
            self.PANEL_ABOVE,
            {"display": "none"},
        )
        self.assertDeclares(".review-panel-jump", self.PANEL_ABOVE, {"display": "inline-flex"})
        self.assertDeclares(".products-layout.panel-closed .review-panel-jump", self.PANEL_ABOVE, {"display": "none"})

    def test_beside_the_list_there_is_nothing_to_jump_to_nor_unfold(self):
        for selector in (".review-panel-jump", ".review-unfold", ".review-unfold-box"):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, None, {"display": "none"})


class TopbarMenuTests(StylesheetTestCase):
    """« Menu » (templates/base.html, static/js/topbar.js, marginmate.css
    « topbar menu »): under 860 px the topbar's links fold into one button,
    once the script has put `topbar-menu-ready` on <html> (the owner, 30/09:
    « the top menu is too big, maybe do something that can be expanded »).
    Measured width by width in accounts/tests/test_topbar_browser.py."""

    FOLDED = "(max-width: 860px)"

    def script(self) -> str:
        return re.sub(r"/\*.*?\*/|//[^\n]*", "", _source("static/js/topbar.js"), flags=re.DOTALL)

    def test_nothing_is_folded_without_the_script(self):
        """JavaScript off, or the file missing: the bar is the old one, every
        link in sight - so every rule of the section's media blocks asks for
        the class the script sets."""
        start, end = stylesheet_section("topbar menu")
        folded = [rule for rule in self.rules if start <= rule.where < end and rule.media]
        self.assertGreater(len(folded), 5)
        offenders = [
            f"{selector} (@media {rule.media})"
            for rule in folded
            for selector in rule.selectors
            if not selector.startswith("html.topbar-menu-ready")
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_button_and_the_section_are_drawn_only_where_the_links_fold(self):
        """Above 860 px, and wherever the script did not run, #topbar-menu is
        no box of its own: its children are the bar's items, as before."""
        for selector in (".topbar-toggle", ".topbar-section"):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, None, {"display": "none"})
        self.assertDeclares(".topbar-menu", None, {"display": "contents"})

    def test_folded_the_bar_leaves_a_one_row_room(self):
        self.assertDeclares("html.topbar-menu-ready", self.FOLDED, {"--topbar-room": "6rem"})

    def test_on_returnables_the_bar_still_leaves_no_room(self):
        """Under 600 px /consignes/'s bar scrolls away with the page: room 0,
        written after the fold's 6rem (same weight: the later wins), and the
        bar `relative`, not `static` - the stacking context the veil and the
        menu are drawn in."""
        returnables = [
            rule
            for rule in self.naming("html:has(.returnables-page)", "(max-width: 600px)")
            if rule.declarations.get("--topbar-room") == "0rem"
        ]
        folded = [
            rule for rule in self.naming("html.topbar-menu-ready", self.FOLDED) if "--topbar-room" in rule.declarations
        ]
        self.assertTrue(returnables and folded)
        self.assertGreater(returnables[-1].where, folded[-1].where)
        self.assertDeclares("html:has(.returnables-page) .topbar", "(max-width: 600px)", {"position": "relative"})

    def test_the_dot_reads_the_badges(self):
        """The dot on « Menu » is read off the badges themselves, so it is
        right after every writer of them - base.html, the review panel's
        out-of-band count, ui.js's to-link-count."""
        self.assertDeclares(".topbar-waiting", None, {"display": "none"})
        self.assertDeclares(".topbar:has(nav .badge) .topbar-waiting", None, {"display": "inline-block"})
        self.assertDeclares(".topbar:has(nav .badge) .topbar-waiting-text", None, {"display": "inline"})

    def test_the_veil_takes_no_finger(self):
        """Opened, the page is veiled by the bar's ::after: a tap on it only
        closes the menu, and a finger dragged on it scrolls nothing."""
        self.assertDeclares(
            'html.topbar-menu-ready .topbar:has(.topbar-toggle[aria-expanded="true"])::after',
            self.FOLDED,
            {"position": "fixed", "inset": "0", "touch-action": "none"},
        )

    def test_the_script_sets_itself_up_once_before_it_listens(self):
        """Run twice, every listener would be there twice, and a tap on
        « Menu » would open the menu and shut it again."""
        source = self.script()
        once = re.search(r'if\s*\(\s*\w+\.classList\.contains\("topbar-menu-ready"\)\s*\)\s*return\s*;', source)
        self.assertIsNotNone(once)
        self.assertLess(once.start(), source.index('classList.add("topbar-menu-ready")'))
        self.assertLess(source.index('classList.add("topbar-menu-ready")'), source.index("addEventListener"))

    def test_the_veil_s_listener_sits_on_the_header(self):
        """iOS Safari sends no click to a document listener from an element
        nothing listens on, and Chrome moves a tap landing on one onto the
        nearest button - « Se déconnecter »: the veil's listener is on the
        header itself, and only a click on the header itself closes. A drag
        on the veil is stopped there too, not by the stylesheet: Chrome does
        not honour touch-action on the bar's ::after, and a finger dragged on
        the veil scrolled the page under the open menu (30/09) - only on the
        header itself, or a finger on the menu could not scroll the menu."""
        source = self.script()
        self.assertRegex(source, r'function header\(\w+\)\s*\{\s*return [^;]*\.closest\("\.topbar"\)')
        self.assertRegex(
            source, r'(\w+) = header\(\w+\);\s*if \(!\1\) return;\s*\1\.addEventListener\("click", onVeil\)'
        )
        self.assertRegex(
            source, r'(\w+) = header\(\w+\);\s*if \(!\1\) return;\s*\1\.removeEventListener\("click", onVeil\)'
        )
        self.assertRegex(source, r"function onVeil\(event\)\s*\{\s*if \(event\.target === event\.currentTarget\)")
        self.assertRegex(source, r'\.addEventListener\("touchmove", onVeilDrag, \{ passive: false \}\)')
        self.assertRegex(source, r'\.removeEventListener\("touchmove", onVeilDrag\)')
        self.assertRegex(
            source,
            r"function onVeilDrag\(event\)\s*\{\s*if \(event\.target === event\.currentTarget\) event\.preventDefault\(\)",
        )

    def test_the_script_never_writes_markup(self):
        source = _source("static/js/topbar.js")
        for pattern in MARKUP_WRITERS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))

    def test_menu_never_gives_way_the_section_then_the_brand_do(self):
        """With a bigger root font (Android's text scaling at 150 or 200 %)
        the one row was wider than a 360-412 px phone and pushed « Menu »,
        the one way to every link, off the screen: the brand could not
        shrink above 340 px (`flex: none`), and the section asked for its
        own width first (`flex: 1 1 auto`). The section now takes only the
        room left (`1 1 0`), the brand shrinks behind it with an ellipsis,
        and « Menu » keeps its size (review of 30/09). Measured at 150 and
        200 % in accounts/tests/test_topbar_browser.py."""
        self.assertDeclares(
            "html.topbar-menu-ready .brand",
            self.FOLDED,
            {
                "flex": "0 1 auto",
                "min-width": "0",
                "overflow": "hidden",
                "text-overflow": "ellipsis",
            },
        )
        self.assertDeclares("html.topbar-menu-ready .topbar-section", self.FOLDED, {"flex": "1 1 0", "min-width": "0"})
        self.assertDeclares("html.topbar-menu-ready .topbar-toggle", self.FOLDED, {"flex": "none"})


class SharedScriptWritesNoMarkupTests(SimpleTestCase):
    def test_the_shared_script_never_writes_markup(self):
        """ui.js builds nodes: its to-link-count handler wrote the Recettes
        badge as markup from the event's data (`innerHTML`), the one writer
        of a badge that did - « Menu »'s dot reads them (30/09)."""
        source = _source("static/js/ui.js")
        for pattern in MARKUP_WRITERS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source))


class NotificationScriptsTests(SimpleTestCase):
    """The notifications' scripts (02/10): the pages' sync on every page of an
    espace, the « Notifications » page's, the login's, and the service
    worker served at /sw.js. ES5 like every script here - a phone's old
    browser parses none of a file holding one arrow - and nodes and text
    only: an endpoint, a device's label and a server's sentence are text."""

    SCRIPTS = (
        "static/js/push_sync.js",
        "static/js/notifications.js",
        "static/js/login_next.js",
        "notifications/service_worker.js",
    )
    #: ES2015 and later, as written in a statement (comments taken out).
    NOT_ES5 = (
        r"=>",
        r"`",
        r"\blet\s+[A-Za-z_$]",
        r"\bconst\s+[A-Za-z_$]",
        r"\basync\s+function",
        r"\bawait\s",
        r"\bclass\s+[A-Z]",
        r"\.\.\.[A-Za-z_$]",
    )

    def code(self, relative: str) -> str:
        return re.sub(r"(?m)^\s*//.*$", "", _blank_comments(_source(relative)))

    def test_each_is_es5_in_a_strict_function(self):
        for relative in self.SCRIPTS:
            code = self.code(relative)
            with self.subTest(script=relative):
                self.assertTrue(code.lstrip().startswith("(function () {"))
                self.assertIn('"use strict";', code)
                self.assertTrue(code.rstrip().endswith("})();"))
            for pattern in self.NOT_ES5:
                with self.subTest(script=relative, pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))

    def test_none_writes_markup(self):
        for relative in self.SCRIPTS:
            source = _source(relative)
            for pattern in MARKUP_WRITERS:
                with self.subTest(script=relative, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source))


class TouchStylesheetTests(StylesheetTestCase):
    """A touch screen is `(pointer: coarse)`, not a width: a phone held
    sideways is wider than 860 px, and a mouse never matches (the « touch »
    section, 30/09)."""

    COARSE = "(pointer: coarse)"

    def test_the_touch_section_is_the_last(self):
        """Last on purpose: a rule there outweighs the same-weight one it
        corrects above. Nothing follows it but its own blocks."""
        start, end = stylesheet_section("touch")
        self.assertEqual(end, len(_source("static/css/marginmate.css")), "a section follows « touch »")
        after = [rule for rule in self.rules if rule.where > start]
        self.assertTrue(after)
        self.assertEqual([rule.selectors for rule in after if "pointer: coarse" not in (rule.media or "")], [])

    def test_touch_fields_are_16_px(self):
        """Under 16 px iOS Safari zooms the whole page in on the field a
        finger taps, and leaves it zoomed - the app's fields were 0.92rem.
        .copy-row input is named: its own 0.85rem outweighs the rule."""
        for selector in ("input", "select", "textarea", ".copy-row input"):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, self.COARSE, {"font-size": "1rem"})

    def test_touch_targets(self):
        """44 px in <main>; 36 in a table's row, a chip or a segmented
        choice - separate selectors, never one :is() list, which weighs as
        its heaviest member and would let the tabs' 44 win in a cell. The
        recipe form's till chips too (`.pick-chip`, each one removes a till
        product): 22 px on a touch screen, a thumb aimed at one removed its
        neighbour (review of 30/09)."""
        for selector in (
            "main .btn",
            "main .link-button",
            "main summary",
            "main .tabs a.tab",
            "main .review-panel-jump",
        ):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, self.COARSE, {"min-height": "44px"})
        for selector in (
            "main td .btn",
            "main th .btn",
            "main td .link-button",
            "main th .link-button",
            "main td summary",
            "main th summary",
            "main .chip",
            "main .segmented button",
            "main .pick-chip",
        ):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, self.COARSE, {"min-height": "36px"})
        mixed = [
            selector
            for rule in self.rules
            if rule.media == self.COARSE and rule.declarations.get("min-height") in ("44px", "36px")
            for selector in rule.selectors
            if ":is(" in selector
        ]
        self.assertEqual(mixed, [])

    def test_the_camera_tile_is_a_touch_screen_s(self):
        """Achats' « Prendre une photo » (01/10): a desktop browser ignores
        `capture`, so the tile is hidden there and drawn on a touch screen -
        never by a width, a phone held sideways being wider than 860 px. The
        hiding rule comes after .upload-choice's display, which weighs the
        same; test_receipt_camera_browser.py looks at both in Chrome."""
        self.assertDeclares(".upload-choice-camera", None, {"display": "none"})
        # Its gap to its previews and the other choices drawn with it: a
        # desktop gets none.
        self.assertDeclares(".upload-choice-camera", self.COARSE, {"display": "grid", "margin-bottom": "var(--s3)"})
        choice = [rule.where for rule in self.naming(".upload-choice") if "display" in rule.declarations]
        camera = [rule.where for rule in self.naming(".upload-choice-camera")]
        self.assertGreater(min(camera), max(choice))
        self.assertEqual(
            [rule.media for rule in self.rules if ".upload-choice-camera" in rule.selectors], [None, self.COARSE]
        )

    def test_a_phone_on_its_side_keeps_no_strip_of_rows(self):
        """Wider than 860 px and some 430 tall: the desktop's table box left
        a strip of rows scrolling inside the page."""
        short = "(pointer: coarse) and (max-height: 600px)"
        self.assertDeclares(".table-wrap", short, {"max-height": "none"})
        self.assertDeclares("thead th", short, {"position": "static"})


class WhatACardHoldsTests(StylesheetTestCase):
    """Inside the phone cards (marginmate.css, « phone cards »), what the
    final review of 30/09 found once they were drawn, each measured in
    Chrome by tests/test_phone_width_browser.py: here, the rules they rest
    on, for the fast loop."""

    PHONE = "(max-width: 860px)"

    def card_rules(self) -> list:
        return [
            rule
            for rule in self.rules
            if rule.media == self.PHONE and any("phone-cards" in selector for selector in rule.selectors)
        ]

    def test_a_bulk_box_is_lifted_out_only_by_the_row_that_holds_it(self):
        """A document's box is taken out of its card's grid (`position:
        absolute`) against its row, which `position: relative` makes its
        containing block - under a :has(). Where :has() is not supported
        (Firefox before 121, Safari before 15.4, Chrome before 105) that
        rule is dropped whole: the box, lifted out by a rule without the
        :has(), was placed against the page, every document's box at one
        spot under the sticky bar, none of them reachable. So the box is
        lifted out by a selector going through the very row that holds it,
        and both rules are dropped together."""
        holding = {
            selector
            for rule in self.card_rules()
            if rule.declarations.get("position") == "relative"
            for selector in rule.selectors
        }
        lifted = [
            selector
            for rule in self.card_rules()
            if rule.declarations.get("position") == "absolute"
            for selector in rule.selectors
        ]
        self.assertTrue(
            any("td.select-col" in selector for selector in lifted), "no rule lifting a bulk box out of its card"
        )
        loose = [selector for selector in lifted if selector.rsplit(" > ", 1)[0] not in holding]
        self.assertEqual(
            loose, [], "lifted out of its card by a rule its row's `position: relative` may be dropped from"
        )

    def test_a_state_s_pill_and_a_till_s_name_wrap_inside_their_card(self):
        """`.status-pill` and `.till-chip` are nowrap (the wide table's), and
        the card's cells break anywhere - which does nothing inside a nowrap
        box: « Fournisseur à confirmer » (154 px) in a 108 px cell ran over
        the next figure at 430 px and past the card at 320; a long till name
        pushed the recipes' box sideways."""
        for selector in ("table.phone-cards .status-pill", "table.phone-cards .till-chip"):
            with self.subTest(selector=selector):
                self.assertDeclares(selector, self.PHONE, {"white-space": "normal"})

    def test_a_table_inside_a_card_keeps_its_words_whole(self):
        """A document's lines open in a table under its card, and inherited
        the card's cells' `overflow-wrap: anywhere`: its columns shrank to
        one letter (« Ligne » on five lines at 320 px, a product's name one
        letter a line) instead of scrolling in its .table-scroll. It keeps
        the page's break-word."""
        self.assertDeclares("table.phone-cards td table", self.PHONE, {"overflow-wrap": "break-word"})

    def test_a_row_with_an_end_cell_always_has_two_tracks(self):
        """The title spans `1 / -2` and the end cell (« Vérifier ») `-2 /
        -1`: both assume two tracks. At 280 px a document's card - its bulk
        box's room taken off - had one, and the button came first, above
        its supplier's name. A track is never more than half of the row,
        the row's column gap taken off."""
        gap = self.declared("table.phone-cards > tbody > tr", self.PHONE)["gap"].split()[-1]
        columns = self.declared("table.phone-cards > tbody > tr:has(> td.phone-card-end)", self.PHONE).get(
            "grid-template-columns", ""
        )
        self.assertIn(f"calc((100% - {gap}) / 2)", columns)
        self.assertIn("auto-fill", columns)


class FocusRingTests(StylesheetTestCase):
    """The app draws one focus ring (`:where(…):focus-visible`, which weighs
    what `:focus-visible` weighs). A rule of the same weight coming later
    with `all: unset` takes it away: « Se déconnecter », the last item of the
    open « Menu », and a table's sort buttons showed no focus at all (review
    of 30/09). accounts/tests/test_topbar_browser.py tabs to the logout,
    tests/test_phone_width_browser.py to a sort button."""

    def test_what_all_unset_restyles_gets_the_ring_back(self):
        (ring,) = [
            rule
            for rule in self.rules
            if rule.media is None
            and any(
                selector.startswith(":where(") and selector.endswith(":focus-visible") for selector in rule.selectors
            )
        ]
        unset = [
            selector for rule in self.rules if rule.declarations.get("all") == "unset" for selector in rule.selectors
        ]
        self.assertEqual(sorted(unset), [".link-button", "th.sortable > button"])
        for selector in unset:
            with self.subTest(selector=selector):
                self.assertDeclares(f"{selector}:focus-visible", None, {"outline": ring.declarations["outline"]})


def template_sources():
    """Every template of the project: (its path from the root, its text)."""
    paths = sorted({*ROOT.glob("templates/**/*.html"), *ROOT.glob("*/templates/**/*.html")})
    return [
        (path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in paths
        if ".venv" not in path.parts
    ]


class SelectAllBoxes(HTMLParser):
    """Each « Tout sélectionner » box (input[data-bulk-all]) of a page or a
    template: the form it ticks for, whether it sits in the header row of a
    table read as cards, the form around it and its label's words."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.open, self.boxes = [], []
        # A template's own comments and tags, as template_tables reads them.
        self.feed(template_markup(source))
        self.close()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and "data-bulk-all" in attributes:
            tables = [index for index, (name, _, _) in enumerate(self.open) if name == "table"]
            nearest = tables[-1] if tables else None
            head = nearest is not None and any(name == "thead" for name, _, _ in self.open[nearest:])
            self.boxes.append(
                {
                    "form": attributes.get("form"),
                    "in_a_card_head": head and "phone-cards" in (self.open[nearest][1].get("class") or "").split(),
                    "inside": next((held.get("id") for name, held, _ in reversed(self.open) if name == "form"), None),
                    "label": next((words for name, _, words in reversed(self.open) if name == "label"), None),
                }
            )
        if tag not in VOID_ELEMENTS:
            self.open.append((tag, attributes, [] if tag == "label" else None))

    def handle_endtag(self, tag):
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                del self.open[index:]
                return

    def handle_data(self, data):
        for _, _, words in self.open:
            if words is not None:
                words.append(data)


class SelectAllOnAPhoneTests(TestCase):
    """« Tout sélectionner » sat in the header row of Achats' documents -
    which a phone does not draw once the rows are cards (`table.phone-cards
    > thead { display: none }`): under 860 px nothing ticked every document
    of an import at once, each card had to be ticked by hand (review of
    30/09). It is in the bulk bar now, as « À vérifier » has it, drawn at
    every width; tests/test_phone_width_browser.py ticks every card through
    it at 375 px. Data invented."""

    def test_no_card_list_hides_its_select_all_in_its_header(self):
        found = [
            f"{name}: « Tout sélectionner » for #{box['form']}"
            for name, source in template_sources()
            for box in SelectAllBoxes(source).boxes
            if box["in_a_card_head"]
        ]
        self.assertEqual(found, [], "\n".join(found))
        # Not a guard of nothing: the bulk bars' boxes were read.
        self.assertGreaterEqual(sum(len(SelectAllBoxes(source).boxes) for _, source in template_sources()), 2)

    def test_the_guard_sees_a_select_all_in_a_card_s_header(self):
        """The parser itself, on the documents' header row as it was."""
        before = (
            '<table class="documents-table phone-cards"><thead><tr><th class="select-col" data-no-sort>'
            '<input type="checkbox" data-bulk-all form="invoice-bulk-form" aria-label="Tout sélectionner"></th>'
            "</tr></thead></table>"
        )
        self.assertEqual([box["in_a_card_head"] for box in SelectAllBoxes(before).boxes], [True])
        wide = before.replace(" phone-cards", "")
        self.assertEqual([box["in_a_card_head"] for box in SelectAllBoxes(wide).boxes], [False])

    def test_purchases_documents_draw_it_in_their_bulk_bar(self):
        cellar = make_supplier(name="Cave Exemple")
        for number in ("F-1", "F-2"):
            make_invoice(supplier=cellar, invoice_number=number)
        response = self.client.get(reverse("invoices:invoice_list"))
        self.assertEqual(response.status_code, 200)
        boxes = [box for box in SelectAllBoxes(response.content.decode()).boxes if box["form"] == "invoice-bulk-form"]
        self.assertEqual(len(boxes), 1, boxes)
        (box,) = boxes
        self.assertFalse(box["in_a_card_head"], box)
        self.assertEqual(box["inside"], "invoice-bulk-form")
        self.assertIsNotNone(box["label"], "a box with no words")
        self.assertEqual(" ".join("".join(box["label"]).split()), "Tout sélectionner")


@tag("browser")
class PieTooltipInBrowserTests(StaticLiveServerTestCase):
    """A category name typed as markup, hovered on the « Dépenses » pie in a
    real (headless) Chrome: the tooltip shows the name as text, and nothing
    the name spells is built into the page.

    The server escapes it (`data-label="&lt;img …"`); what undid that was
    static/js/charts.js reading the attribute back - decoded - into
    `innerHTML`, where the image's `onerror` ran. Tagged "browser":
    `--exclude-tag=browser` for the fast loop. Skipped where Chrome or its
    driver is missing. Data invented."""

    NAME = '<img id="injected" src="nothing-here" onerror="document.title=\'ran\'">'
    # Its flush then fires no post_migrate: recreated content types broke
    # every later class restoring its snapshot (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from invoices.scrapers import website

            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        from bank.models import BankTransaction

        for number, (category, amount) in enumerate(((self.NAME, "600.00"), ("Loyer inventé", "400.00"))):
            BankTransaction.objects.create(
                operation_date=date(2026, 6, 3 + number),
                label=f"PRLV SEPA PAYEUR INVENTE {number} REF/{number:04d}",
                counterparty=f"PAYEUR INVENTE {number}",
                amount=-Decimal(amount),
                kind=BankTransaction.Kind.DEBIT,
                fingerprint=f"pie-tooltip-{number}",
                category=category,
            )
        from tests.runner import log_in_the_browser

        # Every page wants a login: the test tenant's owner.
        log_in_the_browser(self.driver, self.live_server_url)
        self.driver.get(f"{self.live_server_url}{reverse('bank:spending_home')}?du=2026-06-01&au=2026-06-30")

    def hover(self, trigger: str) -> dict:
        """Runs `trigger` on the page, then reads the tooltip back."""
        return self.driver.execute_script(
            trigger
            + """
            var tip = document.querySelector('.chart-pie [data-chart-tooltip]');
            return {
                injected: !!document.getElementById('injected'),
                elements: Array.prototype.map.call(tip.querySelectorAll('*'), function (e) { return e.tagName; }),
                text: tip.textContent,
                title: document.title
            };
            """
        )

    def test_hovering_the_wedge_shows_the_name_as_text(self):
        found = self.hover(
            "var slice = document.querySelector('.chart-pie .chart-slice[data-index=\"0\"]');"
            "slice.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: 50, clientY: 50}));"
        )
        self.assertFalse(found["injected"])
        self.assertNotIn("IMG", found["elements"])
        self.assertIn(self.NAME, found["text"])
        self.assertNotEqual(found["title"], "ran")

    def test_hovering_the_legend_shows_the_name_as_text(self):
        found = self.hover(
            "var item = document.querySelector('.chart-pie [data-legend-for=\"0\"]');"
            "item.dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}));"
        )
        self.assertFalse(found["injected"])
        self.assertNotIn("IMG", found["elements"])
        self.assertIn(self.NAME, found["text"])
        self.assertIn("600.00 €", found["text"])
