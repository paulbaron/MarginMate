"""« Produits & charges » on a phone, read off the markup (30/09).

The owner, looking at the page on his phone: the tables' « columns
overlapping and hard to read », and « I cannot scroll down when I have new
products to classify ». Under 860 px the page's tables are read as cards
(marginmate.css, « phone cards »): the header row is not drawn, and each
figure says what it is through its cell's `data-label` - the column's
header, word for word, drawn above the figure by the CSS. A label that
drifts from its header (a column renamed, one added before it, the charges'
window said one way in the header and another in the rows) says the wrong
thing on a phone and nothing at all anywhere else, which is why every label
is compared with its header here, in each of the page's modes: all time,
two dates, between two counts - and in what a row opens.

And the panel « À classer », above the list on a phone: its first product
only, the others behind « Voir les N autres produits » - a label for a box
that has to stay OUTSIDE what every classification swaps, or the list folds
itself back after each product; the ways between the panel and the list;
the page's head and figures two to a row.

What Chrome does with all of it is test_products_phone_browser.py (tagged
browser, skipped by the fast loop); this is the half the markup can say.
Data invented.
"""

import re
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import UnitChoices
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)

D = Decimal
HTMX = {"HTTP_HX_REQUEST": "true"}

#: What each table's header says between its first column (the card's
#: title) and its last (the card's buttons): the words every card repeats.
ALL_TIME_COLUMNS = ["Acheté", "Vendu", "Unité", "Total HT", "Total TTC"]
PERIOD_COLUMNS = ["Ouverture", "Achats", "Clôture", "Sorti", "Vendu", "Manquant", "Manquant (HT)", "Unité"]


# -- a page as a tree -----------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    @property
    def classes(self) -> list:
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        """What the element says, its whitespace as a browser draws it."""
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, Node):
                    walk(child)
                else:
                    parts.append(child)

        walk(self)
        return " ".join("".join(parts).split())

    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag=None, cls=None, **attrs) -> list:
        return [
            node
            for node in self.iter()
            if (tag is None or node.tag == tag)
            and (cls is None or cls in node.classes)
            and all(node.attrs.get(name.replace("_", "-")) == value for name, value in attrs.items())
        ]

    def child_nodes(self, tag) -> list:
        return [child for child in self.children if isinstance(child, Node) and child.tag == tag]

    def ancestors(self) -> list:
        found, node = [], self.parent
        while node is not None:
            found.append(node)
            node = node.parent
        return found


class Tree(HTMLParser):
    """The page as nested elements - tolerant the way a browser is: an
    element left open (the datalists' <option>) is closed by its parent's
    end tag."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", [], None)
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def tree(response) -> Node:
    return Tree(response.content.decode()).root


def header_texts(table: Node) -> list:
    (thead,) = table.child_nodes("thead")
    (row,) = thead.child_nodes("tr")
    return [cell.text() for cell in row.child_nodes("th")]


def rows_of(table: Node, part="tbody") -> list:
    return [row for section in table.child_nodes(part) for row in section.child_nodes("tr")]


def cells_of(row: Node) -> list:
    return row.child_nodes("td")


def labels_of(row: Node) -> list:
    return [cell.attrs.get("data-label") for cell in cells_of(row)]


# -- the list -------------------------------------------------------------------------------------


class ArticleCardsTestCase(TestCase):
    """Three articles in two categories, two of them bought: the rows every
    card of the list is drawn from."""

    def setUp(self):
        self.url = reverse("inventory:stock_list")
        self.supplier = make_supplier(code="GROSSISTE_TEL", name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        self.flour = make_stock_type(name="Farine Exemple", unit=UnitChoices.KILOGRAM, category="Épicerie")
        # Never bought, never counted: the rows of dashes between two counts.
        self.syrup = make_stock_type(name="Sirop Exemple", unit=UnitChoices.LITRE, category="Spiritueux")
        self.products = {}
        for stock_type, name, equivalent in (
            (self.vodka, "VODKA EXEMPLE 70CL", "0.7"),
            (self.flour, "FARINE EXEMPLE 5KG", "5"),
        ):
            self.products[stock_type.pk] = make_product(
                supplier=self.supplier,
                raw_name=name,
                stock_type=stock_type,
                stock_equivalent=equivalent,
            )
            self.buy(stock_type, date(2026, 1, 10))

    def buy(self, stock_type, on, occurred_on=None):
        invoice = make_invoice(supplier=self.supplier, invoice_date=on)
        line = make_invoice_line(
            invoice=invoice,
            product=self.products[stock_type.pk],
            quantity=6,
            total_ht="72",
            vat_rate=D("0.20"),
        )
        make_movement(
            stock_type=stock_type, invoice_line=line, quantity="6", unit_cost_ht="12", occurred_on=occurred_on
        )

    def stock_tables(self, response) -> list:
        tables = tree(response).find_all("table", cls="stock-table")
        self.assertTrue(tables, "aucune catégorie dans la liste")
        return tables

    def assertEveryRowSaysItsColumns(self, table, columns):
        header = header_texts(table)
        self.assertEqual(header[1:-1], columns)
        rows = [row for row in rows_of(table) if "stock-row" in row.classes]
        self.assertTrue(rows)
        for row in rows:
            labels = labels_of(row)
            with self.subTest(row=cells_of(row)[0].text()):
                self.assertEqual(labels[1:-1], header[1:-1])
                # The card's title and its buttons say what they are themselves.
                self.assertEqual((labels[0], labels[-1]), (None, None))
                self.assertIn("row-actions", cells_of(row)[-1].classes)
        return rows


class TheListAsCardsTests(ArticleCardsTestCase):
    def test_every_figure_of_an_article_carries_its_columns_name(self):
        """Seven columns squeezed into 351 px printed over one another -
        « ARTICLEACHETÉVENDU TOTALUNITÉ TTC » (UX review, 30/09). As a card,
        each figure is drawn under its label, and the label is the header's
        word for word."""
        drawn = []
        for table in self.stock_tables(self.client.get(self.url)):
            self.assertIn("phone-cards", table.classes)
            self.assertNotIn("stock-table-period", table.classes)
            drawn += self.assertEveryRowSaysItsColumns(table, ALL_TIME_COLUMNS)
        self.assertEqual(len(drawn), 3)

    def test_between_two_counts_too(self):
        """Ten columns between two counts, and three ways of drawing a row:
        counted twice (a verdict), counted once (« non compté », no
        verdict), not part of the period at all (dashes). Each branch of
        the template writes its own cells, so each one is read here."""
        opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 1, 12, 0)))
        closing = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        for take, stock_type, counted in (
            (opening, self.vodka, "4"),
            (closing, self.vodka, "3"),
            # Absent from the opening count.
            (closing, self.flour, "2"),
        ):
            make_stock_take_line(
                stock_take=take,
                product=None,
                stock_type=stock_type,
                counted_quantity=counted,
                unit=stock_type.unit,
            )
        self.buy(self.vodka, date(2026, 3, 10), occurred_on=date(2026, 3, 10))
        self.buy(self.flour, date(2026, 3, 12), occurred_on=date(2026, 3, 12))

        response = self.client.get(self.url, {"inventaire": closing.pk})
        drawn = {}
        for table in self.stock_tables(response):
            self.assertIn("phone-cards", table.classes)
            self.assertIn("stock-table-period", table.classes)
            for row in self.assertEveryRowSaysItsColumns(table, PERIOD_COLUMNS):
                drawn[row.attrs["data-stock-type-id"]] = row
        self.assertEqual(set(drawn), {str(self.vodka.pk), str(self.flour.pk), str(self.syrup.pk)})
        # The three branches were all drawn - and checked above.
        self.assertNotIn("non compté", drawn[str(self.vodka.pk)].text())
        self.assertIn("non compté", drawn[str(self.flour.pk)].text())
        self.assertEqual({cell.text() for cell in cells_of(drawn[str(self.syrup.pk)])[1:5]}, {"—"})


# -- the charges ----------------------------------------------------------------------------------


class ChargeCardsTests(TestCase):
    """A water bill (one charge item: the supplier's row is the charge) and
    a landlord whose statement names two charge items (rows of their own
    under his)."""

    def setUp(self):
        self.url = reverse("inventory:stock_list")
        self.water = make_supplier(code="EAU_TEL", name="Eau Exemple", parser_key="", expenses_only=True)
        tap_water = make_product(supplier=self.water, raw_name="EAU", is_expense=True)
        bill = make_invoice(supplier=self.water, invoice_date=date(2026, 2, 5), invoice_number="EAU-2026-02")
        make_invoice_line(invoice=bill, product=tap_water, total_ht="40", vat_rate=D("0.055"))

        self.landlord = make_supplier(code="BAIL_TEL", name="Bailleur Exemple", parser_key="", expenses_only=True)
        statement = make_invoice(supplier=self.landlord, invoice_date=date(2026, 2, 3), invoice_number="BAIL-2026-02")
        for charge_item, amount in (("LOYER EXEMPLE", "500"), ("PROVISION EXEMPLE", "80")):
            product = make_product(supplier=self.landlord, raw_name=charge_item, is_expense=True)
            make_invoice_line(
                invoice=statement, product=product, raw_name=charge_item, total_ht=amount, vat_rate=D("0.20")
            )

    def charges_table(self, response) -> Node:
        (table,) = tree(response).find_all("table", cls="charges-table")
        self.assertIn("phone-cards", table.classes)
        return table

    def test_every_charge_row_names_its_window(self):
        """On a phone the header row is not drawn: each row's count says
        which window it counts over, in the header's own words. « 12 »
        read as every bill there is was the 20/09 confusion (« Free est dit
        avoir 12 documents alors qu'en réalité il y en a plus »), and
        « période » is this page's word for two counts, never two dates."""
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        for parameters, heading in (
            ({}, "Documents (12 mois)"),
            ({"du": "2026-02-01", "au": "2026-02-28"}, "Documents (ces dates)"),
            ({"inventaire": take.pk}, "Documents (période)"),
        ):
            with self.subTest(heading=heading):
                response = self.client.get(self.url, parameters)
                self.assertEqual(response.context["charge_documents_heading"], heading)
                table = self.charges_table(response)
                header = header_texts(table)
                self.assertEqual(header[1:-1], [heading, "Total TTC", "Dernier"])
                suppliers = [
                    row for row in rows_of(table) if "stock-row" in row.classes and "charge-row" not in row.classes
                ]
                self.assertEqual(len(suppliers), 2)
                for row in suppliers:
                    labels = labels_of(row)
                    self.assertEqual(labels[1:-1], header[1:-1])
                    self.assertEqual((labels[0], labels[-1]), (None, None))
                    # 📈 at the end of the title's line.
                    self.assertIn("phone-card-end", cells_of(row)[-1].classes)

    def test_a_charge_item_row_is_labelled_too(self):
        """A charge item has no « Dernier » of its own: that cell stays truly
        empty, which the card does not draw (td:empty), and the two figures
        it has carry the supplier row's labels."""
        table = self.charges_table(self.client.get(self.url))
        header = header_texts(table)
        charge_items = [row for row in rows_of(table) if "charge-row" in row.classes]
        self.assertEqual(
            sorted(cells_of(row)[0].text() for row in charge_items), ["▸LOYER EXEMPLE", "▸PROVISION EXEMPLE"]
        )
        for row in charge_items:
            cells = cells_of(row)
            self.assertEqual(labels_of(row)[1:3], header[1:3])
            self.assertIsNone(cells[3].attrs.get("data-label"))
            self.assertEqual(cells[3].children, [], "« Dernier » d'un poste : une cellule vide, sans même un espace")
            self.assertIn("phone-card-end", cells[-1].classes)


# -- what a row opens -----------------------------------------------------------------------------


class WhatARowOpensTests(ArticleCardsTestCase):
    def test_an_articles_purchases_are_labelled(self):
        """An article's purchases, nine columns wide, scrolled sideways
        inside a row that was itself too narrow: the conversion and
        « Retirer » were off the phone. As cards, each labelled - the
        price's label naming the article's unit as its header does - and
        the conversion asks a phone for its number pad."""
        response = self.client.get(reverse("inventory:stock_type_movements", args=[self.flour.pk]))
        (table,) = tree(response).find_all("table", cls="movements-table")
        self.assertIn("phone-cards", table.classes)
        header = header_texts(table)
        self.assertEqual(
            header[1:-1],
            ["Fournisseur", "Date", "Quantité achetée", "Prix (HT) / Kilogramme", "Total HT", "TVA", "Total TTC"],
        )
        rows = rows_of(table)
        self.assertEqual(len(rows), 1)
        for row in rows:
            labels = labels_of(row)
            self.assertEqual(labels[1:-1], header[1:-1])
            self.assertEqual((labels[0], labels[-1]), (None, None))
            self.assertIn("phone-card-wide", cells_of(row)[-1].classes)
        (factor,) = table.find_all("input", name="stock_equivalent")
        self.assertIn("conversion-form", factor.parent.classes)
        self.assertEqual(factor.attrs.get("inputmode"), "decimal")

    def test_a_charges_documents_are_labelled(self):
        water = make_supplier(code="EAU_TEL2", name="Eau Exemple", parser_key="", expenses_only=True)
        tap_water = make_product(supplier=water, raw_name="EAU", is_expense=True)
        for month in (1, 2):
            bill = make_invoice(supplier=water, invoice_date=date(2026, month, 5), invoice_number=f"EAU-2026-0{month}")
            make_invoice_line(invoice=bill, product=tap_water, total_ht="40", vat_rate=D("0.055"))

        response = self.client.get(reverse("inventory:charge_supplier_documents", args=[water.pk]))
        (table,) = tree(response).find_all("table", cls="charge-documents")
        self.assertIn("phone-cards", table.classes)
        header = header_texts(table)
        self.assertEqual(header[1:-1], ["Date", "HT", "TVA", "TTC"])
        rows = rows_of(table)
        self.assertEqual(len(rows), 2)
        for row in rows:
            labels = labels_of(row)
            self.assertEqual(labels[1:-1], header[1:-1])
            self.assertEqual((labels[0], labels[-1]), (None, None))
            # « Corriger » at the end of the document's line.
            self.assertIn("phone-card-end", cells_of(row)[-1].classes)
        (foot,) = rows_of(table, "tfoot")
        self.assertEqual([label for label in labels_of(foot) if label], ["TTC"])


# -- the panel « À classer » ----------------------------------------------------------------------


class ThePanelOnAPhoneTests(TestCase):
    def setUp(self):
        self.url = reverse("inventory:stock_list")
        self.supplier = make_supplier(code="GROSSISTE_TEL", name="Grossiste Exemple")
        self.vodka = make_stock_type(name="Vodka Exemple", unit=UnitChoices.LITRE, category="Spiritueux")

    def waiting(self, count) -> list:
        products = []
        for number in range(1, count + 1):
            product = make_product(supplier=self.supplier, raw_name=f"PRODUIT EXEMPLE {number:02d}")
            make_invoice_line(invoice=make_invoice(supplier=self.supplier), product=product, quantity=2, total_ht="18")
            products.append(product)
        return products

    def unfold_label(self, page: Node):
        labels = page.find_all("label", **{"for": "review-unfold"})
        self.assertLessEqual(len(labels), 1)
        return labels[0] if labels else None

    def test_four_waiting_the_first_card_and_a_label_for_the_three_others(self):
        """Above the list, up to fifty whole cards (the panel's page) came
        before the first article - screens of them on a phone. The first is
        drawn, and a label unfolds the
        others with no script, through a box that sits OUTSIDE
        #review-panel-body: every classification swaps that, and a box
        inside it would fold the list back after each product."""
        self.waiting(4)
        page = tree(self.client.get(self.url))
        (box,) = page.find_all(id="review-unfold")
        self.assertEqual((box.tag, box.attrs.get("type")), ("input", "checkbox"))
        around = [node.attrs.get("id") for node in box.ancestors()]
        self.assertIn("a-classer", around)
        self.assertNotIn("review-panel-body", around)
        # Every card is still in the page: the fold is the stylesheet's.
        (body,) = page.find_all(id="review-panel-body")
        self.assertEqual(len(body.find_all("article", cls="review-card")), 4)

        label = self.unfold_label(page)
        self.assertIsNotNone(label)
        self.assertIn("Voir les 3 autres produits", label.text())
        self.assertIn("N'afficher que le premier", label.text())
        # Swapped with the cards, so its count follows them.
        self.assertIn(body, label.ancestors())

    def test_two_waiting_say_the_other(self):
        self.waiting(2)
        label = self.unfold_label(tree(self.client.get(self.url)))
        self.assertIn("Voir l'autre produit", label.text())
        self.assertNotIn("Voir les", label.text())

    def test_one_waiting_needs_no_label(self):
        self.waiting(1)
        self.assertIsNone(self.unfold_label(tree(self.client.get(self.url))))

    def test_the_panels_answers_carry_the_label_not_the_box(self):
        """What the page swaps into #review-panel-body - the panel alone,
        the panel after a « Classer », and after its « Annuler » - brings
        its label, never a second box: the one in the page keeps what the
        reader chose."""
        first, _second, _third = self.waiting(3)
        # In this order: « Annuler » sends back what « Classer » classified.
        answers = {
            "le panneau": self.client.get(reverse("inventory:review_queue"), **HTMX),
            "après « Classer »": self.client.post(
                reverse("inventory:assign_product", args=[first.pk]),
                {"stock_type_name": "Vodka Exemple", "stock_equivalent": "0.7"},
                **HTMX,
            ),
            "après « Annuler »": self.client.post(reverse("inventory:remove_product", args=[first.pk]), **HTMX),
        }
        for name, answer in answers.items():
            with self.subTest(answer=name):
                self.assertEqual(answer.status_code, 200)
                self.assertNotContains(answer, "<html")
                self.assertContains(answer, 'for="review-unfold"', count=1)
                self.assertNotContains(answer, 'id="review-unfold"')
        self.assertContains(answers["après « Classer »"], "Voir l'autre produit")
        self.assertContains(answers["après « Annuler »"], "Voir les 2 autres produits")

    def test_the_box_is_named_by_what_it_unfolds_in_both_states(self):
        """A screen reader names the box from its label, whose words flip
        with it (the stylesheet hides the one that does not apply): once
        ticked it read « N'afficher que le premier, case à cocher, cochée »
        - « only the first is shown: on » - with every card drawn (review of
        30/09). The box is named by the « Voir les N autres produits » span
        alone (aria-labelledby reads it even hidden), so « cochée » means
        the others are shown; the other words are hidden from a reader. The
        span comes with every swap of the panel's body, so the name keeps
        its count."""
        first, _second, _third = self.waiting(3)
        page = tree(self.client.get(self.url))
        (box,) = page.find_all(id="review-unfold")
        name_id = box.attrs.get("aria-labelledby")
        self.assertTrue(name_id, "the box takes its name from its label, whose words flip with it")
        (name,) = page.find_all(id=name_id)
        label = self.unfold_label(page)
        self.assertIn(label, name.ancestors())
        self.assertIn("review-unfold-more", name.classes)
        self.assertEqual(name.text(), "Voir les 2 autres produits")
        (other,) = label.find_all(cls="review-unfold-less")
        self.assertEqual(other.attrs.get("aria-hidden"), "true")

        answer = self.client.post(
            reverse("inventory:assign_product", args=[first.pk]),
            {"stock_type_name": "Vodka Exemple", "stock_equivalent": "0.7"},
            **HTMX,
        )
        (name,) = tree(answer).find_all(id=name_id)
        self.assertEqual(name.text(), "Voir l'autre produit")

    def test_only_a_touch_screen_keeps_the_next_field_unfocused(self):
        """After « Classer » the next product's field is focused, except where
        a keyboard comes up on the screen (KEYBOARD_ON_SCREEN). Asked of the
        width too (« max-width: 860px »), it dropped the focus on the page in
        a mouse's window under 860 px - a laptop at 150 % zoom - and the next
        Tab went past the next product (review of 30/09). A touch screen's
        question alone; the browser test classifies in such a window."""
        page = self.client.get(self.url).content.decode()
        queries = re.findall(r'KEYBOARD_ON_SCREEN = window\.matchMedia\("([^"]*)"\)', page)
        self.assertEqual(queries, ["(hover: none) and (pointer: coarse)"])

    def test_the_label_counts_what_it_unfolds_not_the_queue(self):
        """The panel draws the first REVIEW_PANEL_SIZE products (fifty), and
        « … et N autres produits » says the rest, as it always did. The
        label promises the cards it unfolds: counted off the queue, it would
        promise cards the page does not hold."""
        self.waiting(5)
        with mock.patch("inventory.views.REVIEW_PANEL_SIZE", 3):
            page = tree(self.client.get(self.url))
        (body,) = page.find_all(id="review-panel-body")
        self.assertEqual(len(body.find_all("article", cls="review-card")), 3)
        self.assertIn("Voir les 2 autres produits", self.unfold_label(page).text())
        self.assertIn("… et 2 autres produits", body.text())

    def test_the_ways_between_the_panel_and_the_list(self):
        """Above the list, the panel and the articles are screens apart on
        a phone: « La liste ↓ » in the panel's head, « ↑ À classer » in the
        list's toolbar - links, so they work without the page's script."""
        self.waiting(2)
        page = tree(self.client.get(self.url))
        (main,) = page.find_all(cls="products-main")
        self.assertEqual(main.attrs.get("id"), "articles")
        (down,) = page.find_all("a", cls="review-panel-jump", href="#articles")
        self.assertIn("review-panel-body", [node.attrs.get("id") for node in down.ancestors()])
        # The figure « N produits dans le panneau » links there too.
        (up,) = [link for link in page.find_all("a", href="#a-classer") if "data-panel-jump" in link.attrs]
        self.assertIn("review-panel-jump", up.classes)
        self.assertIn("table-toolbar", [cls for node in up.ancestors() for cls in node.classes])
        self.assertIn("2", up.text())

    def test_nothing_waiting_no_way_to_a_panel(self):
        # Read off the elements: the page's script names the attribute.
        page = tree(self.client.get(self.url))
        self.assertEqual([node.tag for node in page.iter() if "data-panel-jump" in node.attrs], [])
        self.assertEqual(page.find_all(cls="review-panel-jump"), [])
        self.assertEqual(page.find_all(id="review-unfold"), [])
        # The list keeps its anchor all the same (a bookmark, a link).
        (main,) = page.find_all(cls="products-main")
        self.assertEqual(main.attrs.get("id"), "articles")


# -- the head and the figures ---------------------------------------------------------------------


class TheHeadOnAPhoneTests(ArticleCardsTestCase):
    def test_the_head_and_the_figures_are_compact(self):
        """The page's four buttons wrapped into ragged rows of three
        widths, and the headline figures came one to a row, some 200 px
        each, before anything to act on: two to a row on a phone."""
        page = tree(self.client.get(self.url))
        (actions,) = [node for node in page.find_all(cls="actions") if "page-header" in node.parent.classes]
        self.assertIn("actions-compact", actions.classes)
        (stats,) = page.find_all(id="stock-stats")
        self.assertEqual(stats.classes, ["stat-row", "stat-row-compact"])

    def test_the_lists_refresh_keeps_the_figures_compact(self):
        """The list reloads itself after every classification and sends
        the figures out of band: the element it sends replaces the page's,
        class and all."""
        response = self.client.get(reverse("inventory:stock_catalogue"), **HTMX)
        (stats,) = tree(response).find_all(id="stock-stats")
        self.assertEqual(stats.attrs.get("hx-swap-oob"), "true")
        self.assertIn("stat-row-compact", stats.classes)
