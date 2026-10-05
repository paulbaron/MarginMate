"""Pages at a phone's width, in a real (headless) Chrome.

« Review the UX/UI on mobile phone » (the owner, 30/09). That day, at
375 px, a ticket's check was 507 px wide (its one grid column grown to the
shop's known prices), a supplier's page 449 (a table straight in a .card, a
button that would not wrap), a recipe's 505 (its ingredients in a .card)
and Produits & charges 551 (its period's select, 16rem and an inventory's
note) - and Chrome then widened the whole page: it zoomed out sideways and
the sticky topbar drifted out of view. The lists a phone opens right after
its photos, Achats' documents and an import's files, and the recipes looked
up at the bar, were tables two to three times as wide as the screen.

* **No page is wider than the phone**, at 320, 375 and 430 px, with what
  makes a page wide: a shop's name printed as one word of 40 letters, a
  known price's long label, an ingredient's cost to four decimals, an
  inventory's note in a select, a product's raw name with no space. A page
  too wide is named with the element the overflow starts from. « Entrées
  d'argent » (01/10) is measured with every list it draws: a payer the bank
  prints as one word in « Autres entrées », a card payout printing no gross,
  a cash deposit and a payer retained - each credit's « En caisse » menu
  beside it - and the card balance's chart. « Trésorerie » (02/10) with
  three balances typed and a gap's card: its adjustment form, its points'
  « Corriger » and « Supprimer », its adjustments' table. « Listes de
  courses » (04/10): the lists' page and its store menu, a list to prepare
  holding a free text printed as one word and a note of 60 letters with no
  space, its « Modifier » card, the tick page - a ticked item, the sticky
  « Courses terminées » - and a finished list; then (05/10) the add form's
  unit select filled by its script, a card's select for an item counted in
  bottles, and « 3 bouteilles de 70 cl » in the store. « Factures de
  vente » (05/10): the tab's list, an electronic invoice whose line is tied
  to a recipe of 60 letters - each select of its grid as wide as that
  option - and a typed one whose line's label is one word of 60 letters.
* **A table's search box sits before the box it scrolls in**, never inside
  it, where it scrolled away with the columns (static/js/datatable.js).
* **A ticket's photo is part of the page** once stacked above its lines:
  no sticky box scrolling on its own under the finger. And **at 1280 px,
  with a mouse, the pages are as they were**: the photo beside the lines,
  words and buttons on their line, the period's 16rem, a table's new box
  drawing nothing.
* **The lists are cards on a phone** (marginmate.css, « phone cards »):
  Achats' documents, « Documents à corriger », an import's files and the
  recipes fit their box, each figure under its header's words; a document's
  bulk box still ticks and its row still opens its lines across the card,
  the recipes' search box still filters - and at 1280 px, with a mouse,
  they are the tables they were. What the final review of 30/09 found
  inside them: « Tout sélectionner » drawn and ticking every card, a
  document's box in its card without :has() too, its first line kept at
  280 px, a long state or till name inside its cell, a document's lines
  never broken inside a word - and, on the laptop, a column's sort button
  reached by Tab drawing the focus ring.

The phone is Chrome's mobile emulation with touch (the meta viewport, the
coarse pointer), as returnables/tests/test_phone_browser.py sets it; the
laptop has neither. Every setting is undone after each test: the class
shares one Chrome.

Tagged "browser": `--exclude-tag=browser` for the fast loop; run with the
cached chromedriver (webdriver-manager looks the latest one up online).
Skipped where Chrome or its driver is missing. Data invented.
"""

import tempfile
from datetime import date, datetime
from decimal import Decimal

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse
from django.utils import timezone

from inventory.models import GapFillEntry, ShoppingListItem, UnitChoices
from invoices.scrapers import website
from tests.factories import (
    make_ingredient,
    make_invoice,
    make_invoice_line,
    make_movement,
    make_priced_stock_type,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_take_line,
    make_stock_type,
    make_supplier,
)
from tests.runner import log_in_the_browser
from tests.test_views_smoke import (
    SHOPPING_BEER_PRODUCT,
    SHOPPING_GIN_PRODUCT,
    make_shopping_bottles,
    make_shopping_history,
    make_shopping_lists,
)

#: The widths of the review: a small phone, the owner's, a large one.
WIDTHS = (320, 375, 430)
HEIGHT = 800
CHECKED = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]
#: A shop's name as some tickets print it: one word, 40 letters, no space to
#: break at - in the page's title, a breadcrumb, a button, a heading.
ONE_WORD = "EPICERIEDUQUARTIEREXEMPLEDISTRIBUTIONSAS"
#: The period's select is as wide as its longest option, which names the take.
LONG_NOTE = "Inventaire de fin de saison, cave et réserve comprises, compté à deux"
#: A payer the bank prints as one word, 42 letters: a credit of « Autres
#: entrées », its name above its label and its « En caisse » menu beside it.
LONG_PAYER = "COMITEDESFETESDUQUARTIEREXEMPLEASSOCIATION"
#: A payment terminal whose payouts print no gross, its payer retained as
#: « Carte » on « Entrées d'argent ».
TERMINAL = "TERMINAL EXEMPLE ENCAISSEMENTS"
#: A shopping list item's note typed as one word of 60 letters: in its cell,
#: under its tick in the store.
LIST_NOTE = f"{ONE_WORD}{ONE_WORD[:20]}"
#: A recipe's name of 60 letters, in a « facture de vente »'s select of what
#: a line sold (a <select> is as wide as its longest option).
LONG_RECIPE = ("Formule cocktail signature de la maison exemple " * 2)[:60]


def credit_row(day, bank_type, label, amount):
    """A credit as the bank's CSV export prints it (bank/statements.py)."""
    return f"{day:%d/%m/%Y};{bank_type};{bank_type};{label};{day:%d/%m/%Y};{amount}"


#: Where a page too wide starts to be: each element past the screen's right
#: edge (arguments[0] px) whose parent is not, and that no box scrolling or
#: clipping sideways holds - the table in a card, the select in its row.
CULPRITS = r"""
var limit = arguments[0] + 1, found = [];
function held(element) {
    for (var box = element.parentElement; box && box !== document.documentElement; box = box.parentElement) {
        if (getComputedStyle(box).overflowX !== "visible") return true;
    }
    return false;
}
document.querySelectorAll("body *").forEach(function (element) {
    var box = element.getBoundingClientRect();
    if (!box.width || box.right <= limit || held(element)) return;
    if (element.parentElement.getBoundingClientRect().right > limit) return;
    found.push(element);
});
return found.slice(0, 6).map(function (element) {
    var box = element.getBoundingClientRect();
    var classes = typeof element.className === "string" && element.className.trim()
        ? "." + element.className.trim().split(/\s+/).join(".") : "";
    var text = (element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
    return element.tagName.toLowerCase() + (element.id ? "#" + element.id : "") + classes
        + " (" + Math.round(box.width) + " px, right edge at " + Math.round(box.right) + ", « " + text + " »)";
});
"""

#: Each drawn element matching arguments[0] (a pill, a till's name) against
#: the cell it is in: named when it runs past either side of it.
OUT_OF_ITS_CELL = r"""
var found = {drawn: [], out: []};
document.querySelectorAll(arguments[0]).forEach(function (element) {
    if (!element.getClientRects().length) return;
    var box = element.getBoundingClientRect(), cell = element.closest("td").getBoundingClientRect();
    var text = element.textContent.replace(/\s+/g, " ").trim();
    found.drawn.push(text);
    if (box.left < cell.left - 0.5 || box.right > cell.right + 0.5) {
        found.out.push("« " + text + " » " + Math.round(box.left) + "-" + Math.round(box.right)
            + " in a cell " + Math.round(cell.left) + "-" + Math.round(cell.right));
    }
});
return found;
"""

#: Every word drawn inside arguments[0], and those drawn over more than one
#: line - broken inside, where a line may only break between words. A word
#: not drawn is not counted: a table out of sight breaks no word, and
#: `words` then says so. A new line is a piece of the word sitting more
#: than half a line under the one before - never a sub-pixel between two
#: runs of one line (a glyph from another font), which rounding the tops
#: could split in two.
BROKEN_WORDS = r"""
var found = {words: 0, broken: []};
var walker = document.createTreeWalker(document.querySelector(arguments[0]), NodeFilter.SHOW_TEXT);
while (walker.nextNode()) {
    var node = walker.currentNode, word = /\S+/g, match;
    while ((match = word.exec(node.textContent))) {
        var range = document.createRange();
        range.setStart(node, match.index);
        range.setEnd(node, match.index + match[0].length);
        var pieces = Array.prototype.filter.call(range.getClientRects(), function (r) { return r.width && r.height; })
            .sort(function (a, b) { return a.top - b.top; });
        if (!pieces.length) continue;
        found.words++;
        var lines = 1;
        for (var i = 1; i < pieces.length; i++) {
            if (pieces[i].top - pieces[i - 1].top > pieces[i - 1].height / 2) lines++;
        }
        if (lines > 1) found.broken.push(match[0] + " (" + lines + " lines)");
    }
}
return found;
"""

#: A document's card's first line, row by row (arguments[0]): its bulk box
#: before its title and on its line, « Vérifier » / « Corriger » at the end
#: of the same line - and the tracks its grid has.
FIRST_LINE = r"""
function across(a, b) { return a.top < b.bottom && a.bottom > b.top; }
return Array.prototype.map.call(document.querySelectorAll(arguments[0]), function (row) {
    var box = row.querySelector("td.select-col input").getBoundingClientRect();
    var title = row.querySelector("td.select-col + td").getBoundingClientRect();
    var end = row.querySelector("td.phone-card-end").getBoundingClientRect();
    var problems = [];
    if (box.right > title.left + 0.5) problems.push("the box is not before the title");
    if (!across(box, title)) problems.push("the box is not on the title's line");
    if (!across(title, end)) problems.push("the button is not on the title's line");
    if (end.left < title.right - 0.5) problems.push("the button is not after the title");
    return {title: row.querySelector("td.select-col + td").textContent.trim(),
            tracks: getComputedStyle(row).gridTemplateColumns, problems: problems};
});
"""

#: A browser without :has() (Firefox before 121, Safari before 15.4,
#: Chrome before 105) drops every rule whose selectors name it: those rules
#: taken out of the page's stylesheets, how many.
WITHOUT_HAS = r"""
var dropped = 0;
function drop(parent) {
    for (var index = parent.cssRules.length - 1; index >= 0; index--) {
        var rule = parent.cssRules[index];
        if (rule.selectorText !== undefined && rule.selectorText.indexOf(":has(") >= 0) {
            parent.deleteRule(index);
            dropped++;
        } else if (rule.cssRules && rule.cssRules.length) {
            drop(rule);
        }
    }
}
Array.prototype.forEach.call(document.styleSheets, function (sheet) {
    try { drop(sheet); } catch (error) { /* another origin's: none here */ }
});
return dropped;
"""


class PhoneBrowserTestCase(StaticLiveServerTestCase):
    """One headless Chrome for the class, logged in as the test tenant's
    owner, a phone or a laptop at will."""

    # Its flush then fires no post_migrate (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
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
        super().setUp()
        log_in_the_browser(self.driver, self.live_server_url)
        # On this site's login page: the next test reads what the last one
        # left - a search typed (datatable.js keeps it for the session).
        self.script("localStorage.clear(); sessionStorage.clear();")
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.clearDeviceMetricsOverride", {})
        self.addCleanup(self.driver.execute_cdp_cmd, "Emulation.setTouchEmulationEnabled", {"enabled": False})

    # -- helpers ------------------------------------------------------------------------------------

    def as_a_phone(self, width, height=HEIGHT):
        """A phone `width` px wide: its meta viewport honoured, a finger."""
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": True},
        )
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

    def as_a_laptop(self, width=1280, height=900):
        """A laptop's window: a mouse, no meta viewport."""
        self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def element(self, css):
        return self.driver.find_element("css selector", css)

    def too_wide(self, width) -> str:
        """ "" when the page fits `width`; else how wide it is and why."""
        page = self.script("return document.documentElement.scrollWidth;")
        if page <= width:
            return ""
        culprits = self.script(CULPRITS, width)
        return f"{page} px wide: " + (" ; ".join(culprits) or "no element named")

    def cards(self, table_css) -> dict:
        """How a table is drawn: its display, its header's, its first body
        row's, and whether it fits the box around it."""
        return self.script(
            "var table = document.querySelector(arguments[0]), box = table.parentElement;"
            "var row = table.tBodies[0].rows[0];"
            "return {table: getComputedStyle(table).display, head: getComputedStyle(table.tHead).display,"
            "  row: getComputedStyle(row).display,"
            "  fits: box.scrollWidth <= box.clientWidth && table.getBoundingClientRect().right <= box.getBoundingClientRect().right + 0.5};",
            table_css,
        )

    def label_drawn(self, cell_css) -> str:
        """The words a card draws above a figure: its cell's ::before."""
        return self.script(
            "return getComputedStyle(document.querySelector(arguments[0]), '::before').content;", cell_css
        )

    def photo_box(self) -> dict:
        """A ticket's check: how its photo's box is drawn, and how many
        columns the page has (the photo beside the lines, or above them)."""
        return self.script(
            "var style = getComputedStyle(document.querySelector('.receipt-photo')),"
            "    columns = getComputedStyle(document.querySelector('.receipt-review')).gridTemplateColumns;"
            "return {position: style.position, overflowY: style.overflowY, maxHeight: style.maxHeight,"
            "  columns: columns.trim().split(/\\s+/).length};"
        )


@tag("browser")
class NoPageWiderThanAPhoneInBrowserTests(PhoneBrowserTestCase):
    def setUp(self):
        super().setUp()
        from bank import income, reconcile
        from bank.models import BankTransaction, IncomePayer, IncomeSource
        from bank.tests.test_reconcile import statement
        from invoices.models import ShopItemPrice
        from recipes.models import PosDailyPayment
        from recipes.sales import record_sales

        # A ticket to check, from a shop printing its name as one word, whose
        # price list is open (a price is known) and its label long.
        self.shop = make_supplier(
            code="EPICERIE_X",
            name=ONE_WORD,
            parser_key="",
            ticket_identifiers=["siren:900000019", "web:epicerie-du-quartier-exemple.fr"],
        )
        self.ticket = make_invoice(supplier=self.shop, invoice_number="T-000042", parse_checks=CHECKED)
        for number, name in enumerate(("CITRONS VERTS EXEMPLE FILET 1KG", "MENTHE FRAICHE EXEMPLE BOTTE")):
            make_invoice_line(
                invoice=self.ticket,
                product=make_product(supplier=self.shop, raw_name=name),
                quantity=number + 2,
                total_ht=f"{12 + number}.34",
                vat_rate=Decimal("0.055"),
            )
        ShopItemPrice.objects.create(
            supplier=self.shop,
            unit_price_ttc=Decimal("0.70"),
            label="Citron vert en filet de cinq pièces, calibre moyen, origine exemple",
        )
        # A recipe whose ingredient is costed to four decimals.
        liqueur = make_priced_stock_type(name="Liqueur de fleur de sureau artisanale exemple", unit_cost_ht="23.4567")
        self.recipe = make_recipe(name="Spritz au sureau exemple", selling_price_ttc="9.50")
        make_ingredient(self.recipe, stock_type=liqueur, quantity="0.0400", group=0)
        # Produits & charges: an article bought, a product waiting with no
        # space in its name, two counts - the second noted at length - and
        # sales past what left the shelf between them, so the « Écarts » page
        # draws its « Incohérences ».
        wholesaler = make_supplier(name="Grossiste Exemple des Boissons")
        rum = make_stock_type(name="Rhum ambré exemple de la maison", unit=UnitChoices.LITRE, category="Spiritueux")
        bottle = make_product(
            supplier=wholesaler, raw_name="RHUM AMBRE EXEMPLE 70CL", stock_type=rum, stock_equivalent="0.7"
        )
        invoice = make_invoice(supplier=wholesaler, invoice_number="FA-2026-000123", invoice_date=date(2026, 6, 3))
        line = make_invoice_line(invoice=invoice, product=bottle, quantity=12, total_ht="1234.56")
        make_movement(
            stock_type=rum, invoice_line=line, quantity="8.4", unit_cost_ht="146.9714", occurred_on=date(2026, 6, 3)
        )
        make_product(supplier=wholesaler, raw_name="REMBOURSEMENTDEPOTGARANTIEEXEMPLEFUT30L")
        punch = make_recipe(name="Punch exemple de la maison", selling_price_ttc="7.00")
        make_ingredient(punch, stock_type=rum, quantity="0.04", group=0)
        self.opening = opening = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 6, 1, 23, 0)))
        make_stock_take_line(stock_take=opening, stock_type=rum, counted_quantity="1", unit=UnitChoices.LITRE)
        self.take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 6, 30, 23, 0)), note=LONG_NOTE)
        make_stock_take_line(stock_take=self.take, stock_type=rum, counted_quantity="9", unit=UnitChoices.LITRE)
        record_sales([("Punch exemple de la maison", date(2026, 6, 10), 40)])
        # « Combler les écarts » from the first count, with a list of two
        # amounts (ten punches each): the last one's sales to ring up, the
        # one before folded under it, the gaps with the list's columns.
        for amount in ("70", "70"):
            added = self.client.post(
                reverse("inventory:stock_gap_filler_add"), {"depuis": self.opening.pk, "montant": amount}
            )
            self.assertEqual(added.status_code, 302)
        self.assertEqual(GapFillEntry.objects.filter(stock_take=self.opening).count(), 2)
        # « Entrées d'argent » over June: the till's card days, then a payout
        # printing its gross and one from a terminal printing none - its
        # payer retained as « Carte » -, a cash deposit, and a payer printed
        # as one word in « Autres entrées ». Every list of the page is drawn,
        # each credit's « En caisse » menu beside it, and the card balance
        # with its chart (two payout days).
        for day, card in ((1, "120.00"), (2, "80.00"), (4, "60.00")):
            PosDailyPayment.objects.create(
                sold_on=date(2026, 6, day), method=PosDailyPayment.CARD, amount=Decimal(card), payments=3
            )
        reconcile.import_statement(
            statement(
                credit_row(
                    date(2026, 6, 3),
                    "VIREMENT",
                    "VIR SEPA RECU /FRM BAR EXEMPLE /EID /RNF TRANSFERT PRESTATAIRE INVENTE 0000001 "
                    "TOTAL ENCAISSE 200.00 EUROS BAR EXEMPLE",
                    "198,60",
                ),
                credit_row(
                    date(2026, 6, 5), "VIREMENT", f"VIR SEPA RECU /FRM {TERMINAL} /EID /RNF VERSEMENT 0000002", "60,00"
                ),
                credit_row(date(2026, 6, 10), "VERSEMENT ESPECES", "VERSEMENT ESPECES 0000003", "40,00"),
                credit_row(
                    date(2026, 6, 20), "VIREMENT", f"VIR SEPA RECU /FRM {LONG_PAYER} /EID /RNF PRIVATISATION", "450,00"
                ),
            )
        )
        terminal = BankTransaction.objects.get(counterparty=TERMINAL)
        IncomePayer.objects.create(key=income.payer_key(terminal), source=IncomeSource.CARD)
        self.income = f"{reverse('bank:income_home')}?du=2026-06-01&au=2026-06-30"
        # « Trésorerie » over those June lines: 01/06 1 000,00; 08/06
        # 1 258,60, what the operations explain; 15/06 12 345,60 - a gap to
        # resolve, its card drawn - and an adjustment counting nowhere.
        from bank.models import TreasuryAdjustment, TreasuryCheckpoint

        for day, balance in ((1, "1000.00"), (8, "1258.60"), (15, "12345.60")):
            TreasuryCheckpoint.objects.create(date=date(2026, 6, day), balance=Decimal(balance))
        TreasuryAdjustment.objects.create(
            date=date(2026, 5, 20), amount=Decimal("-1234.56"), reason="Frais de tenue de compte exemple"
        )
        self.treasury = f"{reverse('bank:treasury')}?tout=1"
        # « Prévoir les courses » with every section it draws (invented),
        # and « Rythme d'achat » for one store: its eleven columns as cards.
        shopping = make_shopping_history()
        self.shopping = f"{reverse('inventory:shopping_list')}?fournisseur={shopping.wholesaler.pk}"
        self.rhythm = f"{reverse('inventory:shopping_rhythm')}?fournisseur={shopping.wholesaler.pk}"
        # « Listes de courses » (04/10): the wholesaler's open list - its
        # syrup ticked, so a struck-through tick and a plain one are drawn -
        # with a free text printed as one word and a note of 60 letters with
        # no space, and the grocer's list finished (all invented).
        self.shopping_lists = lists = make_shopping_lists(shopping)
        ShoppingListItem.objects.create(shopping_list=lists.open, label=ONE_WORD, quantity=Decimal("1"), note=LIST_NOTE)
        # Bottles (04/10): the gin bought here in 70 cl bottles, three of
        # them on the list - its card's unit select, its words in the store.
        bottles = make_shopping_bottles(shopping)
        gin = ShoppingListItem.objects.create(
            shopping_list=lists.open,
            stock_type=bottles.gin,
            label=bottles.gin.name,
            quantity=Decimal("3"),
            product_name=SHOPPING_GIN_PRODUCT,
            pack_size=6,
            item_size=Decimal("0.7"),
            size_unit=UnitChoices.LITRE,
        )
        list_page = reverse("inventory:shopping_list_page")
        self.lists_index = reverse("inventory:shopping_lists")
        self.list_edit = f"{list_page}?fournisseur={shopping.wholesaler.pk}"
        self.list_card = f"{self.list_edit}&ligne={lists.beer.pk}#modifier"
        self.list_gin_card = f"{self.list_edit}&ligne={gin.pk}#modifier"
        self.list_run = f"{self.list_edit}&mode=courses"
        self.list_finished = f"{list_page}?liste={lists.finished.pk}"
        self.wholesaler_name = shopping.wholesaler.name
        # « Factures de vente » (05/10): the tab listing them, an electronic
        # invoice whose line is tied to a recipe of 60 letters, and a typed
        # one whose line's label is one word of 60 letters (all invented).
        from recipes.models import SaleDocument
        from tests.factories import make_sale_document, make_sale_line

        signature = make_recipe(name=LONG_RECIPE, selling_price_ttc="9.50")
        sale = make_sale_document(
            reference="FV-2026-0101",
            customer=ONE_WORD,
            sold_on=date(2026, 6, 20),
            einvoice_format="CII",
            einvoice_type_code="380",
            stated_total_ttc="202.80",
            stated_total_ht="169.00",
            counting=SaleDocument.Counting.COUNTED,
        )
        make_sale_line(sale, label=LONG_RECIPE, quantity="2", total_ht="169.00", vat_rate="0.20", recipe=signature)
        typed = make_sale_document(reference="FV-2026-0102", customer="Mariage Exemple", sold_on=date(2026, 6, 21))
        make_sale_line(typed, label=LIST_NOTE, unit_price_ttc="150")
        self.sales = f"{reverse('recipes:sales_list')}?du=2026-06-01&au=2026-06-30"
        self.sale_einvoice = reverse("recipes:sale_document_update", args=[sale.pk])
        self.sale_typed = reverse("recipes:sale_document_update", args=[typed.pk])

    def test_no_page_is_wider_than_the_phone(self):
        """Measured on the code of 29/09 with this data, every page but
        Achats was wider than the phone at every width: a ticket's check
        514 px and the supplier's page 517 (the name in their breadcrumb,
        « + Nouvelle source pour » it), a recipe 505 (its ingredients),
        Produits & charges 743 either way (the period's select), the
        recipes 459 (the article picker), « Écarts » 439 (« Incohérences »)
        - and Achats 341 at 320 (its two file fields). « Entrées d'argent »,
        added on 01/10 with its one-word payer and every « En caisse » form,
        fitted every width that day: its lists scroll in their own boxes."""
        stock = reverse("inventory:stock_list")
        pages = {
            "le contrôle d'un ticket": reverse("invoices:receipt_review", args=[self.ticket.pk]),
            "la fiche d'un fournisseur": reverse("invoices:supplier_detail", args=[self.shop.pk]),
            "une recette": reverse("recipes:recipe_detail", args=[self.recipe.pk]),
            "Produits & charges": stock,
            "Produits & charges entre deux inventaires": f"{stock}?inventaire={self.take.pk}",
            "Factures": reverse("invoices:invoice_list"),
            "les recettes": reverse("recipes:recipe_list"),
            "les écarts d'un inventaire": reverse("inventory:stock_take_variance", args=[self.take.pk]),
            # From the first count, with its list (setUp): the punches sold
            # since are sales to draw, and the select names the second count
            # by its long note.
            "Combler les écarts": f"{reverse('inventory:stock_gap_filler')}?depuis={self.opening.pk}",
            "les entrées d'argent": self.income,
            "la trésorerie": self.treasury,
            "Prévoir les courses": self.shopping,
            "le rythme d'achat": self.rhythm,
            "les listes de courses": self.lists_index,
            "une liste à préparer": self.list_edit,
            "une ligne à modifier": self.list_card,
            "une ligne en bouteilles à modifier": self.list_gin_card,
            "les courses à cocher": self.list_run,
            "une liste terminée": self.list_finished,
            "les factures de vente": self.sales,
            "une facture de vente électronique": self.sale_einvoice,
            "une facture de vente saisie": self.sale_typed,
        }
        problems = []
        for width in WIDTHS:
            self.as_a_phone(width)
            for name, path in pages.items():
                self.open(path)
                wide = self.too_wide(width)
                if wide:
                    problems.append(f"{name} ({path}), {width} px : {wide}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_pages_draw_what_makes_them_wide(self):
        """The sweep above is only worth its data: the shop's one-word name
        in the ticket's title and on the supplier's button, the known
        prices' table and its long label, the supplier's identifiers, the
        recipe's ingredients, the period's select holding the note, the
        product waiting in « À classer » with no space in its name, and the
        « Incohérences » - not the other table of « Écarts » - are on the
        page (on the code of 29/09 too: it is the data that is checked here).
        And on « Entrées d'argent »: the one-word payer, the « En caisse »
        menu of « Autres entrées » and of the deposits, the payout printing
        no gross, the payer retained and the balance's chart."""
        self.as_a_phone(375)
        ticket = reverse("invoices:receipt_review", args=[self.ticket.pk])
        supplier = reverse("invoices:supplier_detail", args=[self.shop.pk])
        stock = reverse("inventory:stock_list")
        for path, css, words in (
            (ticket, "main", ONE_WORD),
            (
                ticket,
                "details.known-prices[open] table[data-table-label='prix']",
                "Citron vert en filet de cinq pièces",
            ),
            (supplier, "table[data-table-label='identifiants']", "900 000 019"),
            (supplier, "main a.btn[href*='fournisseur=']", ONE_WORD),
            (
                reverse("recipes:recipe_detail", args=[self.recipe.pk]),
                ".card table.sub-table",
                "Liqueur de fleur de sureau",
            ),
            (stock, "#period-select", LONG_NOTE),
            (stock, "#a-classer .review-card", "REMBOURSEMENTDEPOTGARANTIEEXEMPLEFUT30L"),
            (
                reverse("inventory:stock_take_variance", args=[self.take.pk]),
                ".card .table-scroll > table.sub-table, .card > table.sub-table",
                "Exigé par les ventes",
            ),
            (self.income, "table[data-table-label='autres entrées']", LONG_PAYER),
            (
                self.income,
                "table[data-table-label='autres entrées'] form.income-source",
                "retenir pour ce payeur",
            ),
            (
                self.income,
                "table[data-table-label='dépôts et autres moyens de paiement'] form.income-source",
                "retenir pour ce payeur",
            ),
            (self.income, "table[data-table-label='versements carte']", "montant reçu"),
            (self.income, "table[data-table-label='payeurs retenus']", TERMINAL),
            (self.income, ".chart[data-chart='line']", "05/06/2026"),
            (self.treasury, "#ecarts .card form.inline-form", "Ajouter un ajustement de"),
            (self.treasury, "table[data-table-label='soldes saisis']", "à résoudre"),
            (self.treasury, "table[data-table-label='ajustements']", "ne compte pas"),
            (self.shopping, "table[data-table-label='à acheter']", SHOPPING_BEER_PRODUCT),
            (self.shopping, "#exclusions", "Exclure la catégorie « Consignes exemple »"),
            (self.rhythm, 'table[data-table-label="rythme d\'achat"]', "Habitude ici"),
            (self.lists_index, "form.inline-form select", self.wholesaler_name),
            (self.list_edit, "table[data-table-label='articles']", LIST_NOTE),
            (self.list_edit, "table[data-table-label='articles']", ONE_WORD),
            (self.list_card, "#modifier", "Bière exemple"),
            (self.list_gin_card, "#modifier select[name='unite']", "bouteilles de 70 cl"),
            (self.list_run, "#courses .shopping-tick-name", ONE_WORD),
            (self.list_run, "#courses .shopping-tick-quantity", "3 bouteilles de 70 cl"),
            (self.list_run, "#courses .shopping-tick.is-ticked", "Sirop exemple"),
            (self.list_run, "form.shopping-finish", "Garder les articles non pris"),
            (self.list_finished, "table[data-table-label='courses terminées']", "Citron exemple"),
            (self.sales, "table[data-table-label='factures de vente']", ONE_WORD),
            (self.sale_einvoice, "table[data-table-label='lignes'] select", LONG_RECIPE),
            (self.sale_typed, "#sale-line-rows input[name$='-label']", LIST_NOTE),
            (self.sale_typed, "#sale-line-rows select", LONG_RECIPE),
        ):
            with self.subTest(page=path, css=css):
                self.open(path)
                # A field's text is its value: a typed line's « Libellé ».
                texts = self.script(
                    "return Array.from(document.querySelectorAll(arguments[0]))"
                    ".map(function (e) { return e.tagName === 'INPUT' ? e.value : e.textContent; });",
                    css,
                )
                self.assertTrue(any(words in text for text in texts), f"{css}: no « {words} » in {texts!r:.300}")

    def test_the_add_form_s_unit_select_fits_once_filled(self):
        """A name typed in the list's add form: entry_units.js fills its unit
        select - « bouteilles de 70 cl » / « litres » for the gin, the
        longest label « litres (format inconnu) » for the juice -, and the
        page still fits the phone at every width."""
        problems = []
        for width in WIDTHS:
            self.as_a_phone(width)
            self.open(self.list_edit)
            for typed, labels in (
                ("Gin exemple (article)", ["bouteilles de 70 cl", "litres"]),
                ("Jus exemple (article)", ["litres (format inconnu)"]),
            ):
                with self.subTest(width=width, typed=typed):
                    self.script(
                        "var field = document.querySelector('form.shopping-add input[name=\"nom\"]');"
                        "field.value = arguments[0];"
                        "field.dispatchEvent(new Event('input', {bubbles: true}));",
                        typed,
                    )
                    drawn = self.script(
                        "return Array.prototype.map.call("
                        "document.querySelector('form.shopping-add select[name=\"unite\"]').options,"
                        "function (option) { return option.textContent; });"
                    )
                    self.assertEqual(drawn, labels)
                    wide = self.too_wide(width)
                    if wide:
                        problems.append(f"« {typed} », {width} px : {wide}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_a_ticket_s_photo_is_part_of_the_page(self):
        """Stacked above the lines (900 px and under), a ticket's photo is no
        sticky box as tall as the screen scrolling on its own - a second
        page inside the page, where a finger on the receipt scrolled the
        photo and not the lines (« there are scroll issues with some
        panels », 30/09). Read off the page Chrome draws, so a heavier rule
        written elsewhere could not quietly put the box back."""
        self.as_a_phone(375)
        self.open(reverse("invoices:receipt_review", args=[self.ticket.pk]))
        self.assertEqual(
            self.photo_box(), {"position": "static", "overflowY": "visible", "maxHeight": "none", "columns": 1}
        )

    def test_on_a_laptop_the_pages_are_as_they_were(self):
        """With a mouse at 1280 px nothing of the phone's rules applies - the
        review of 30/09 changed nothing above 860 px: the ticket's photo is
        the sticky box beside its lines, scrolling on its own; a word and a
        button keep their line; the period's select keeps its 16rem; the
        box a recipe's table now sits in draws nothing around it; and no
        page is wider than the window. As on 29/09."""
        self.as_a_laptop()
        ticket = reverse("invoices:receipt_review", args=[self.ticket.pk])
        supplier = reverse("invoices:supplier_detail", args=[self.shop.pk])
        stock = reverse("inventory:stock_list")
        recipe = reverse("recipes:recipe_detail", args=[self.recipe.pk])

        self.open(ticket)
        photo = self.photo_box()
        self.assertEqual((photo["position"], photo["overflowY"], photo["columns"]), ("sticky", "auto", 2))
        self.assertEqual(self.too_wide(1280), "")

        self.open(supplier)
        self.assertEqual(
            self.script(
                "var name = arguments[0], button = Array.from(document.querySelectorAll('main a.btn')).filter(function (b) {"
                "  return b.textContent.indexOf(name) >= 0; })[0];"
                "return [getComputedStyle(document.body).overflowWrap, button && getComputedStyle(button).whiteSpace];",
                ONE_WORD,
            ),
            ["normal", "nowrap"],
        )
        self.assertEqual(self.too_wide(1280), "")

        self.open(stock)
        self.assertEqual(
            self.script("return getComputedStyle(document.getElementById('period-select')).minWidth;"), "256px"
        )
        self.assertEqual(self.too_wide(1280), "")

        self.open(recipe)
        gaps = self.script(
            "var table = document.querySelector('.card table.sub-table'), card = table.closest('.card'),"
            "    style = getComputedStyle(card), box = card.getBoundingClientRect(), drawn = table.getBoundingClientRect();"
            "return [drawn.left - box.left - parseFloat(style.borderLeftWidth) - parseFloat(style.paddingLeft),"
            "  box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight) - drawn.right,"
            "  table.parentElement.scrollWidth - table.parentElement.clientWidth];"
        )
        for gap in gaps:
            self.assertAlmostEqual(gap, 0, delta=0.5, msg=f"the recipe's table against its card: {gaps}")
        self.assertEqual(self.too_wide(1280), "")

    def test_a_table_s_search_box_sits_before_its_scroll_box(self):
        """Inside the box, the search box scrolled away sideways with the
        columns; before it, it stays where the thumb is."""
        self.as_a_phone(375)
        self.open(reverse("invoices:supplier_detail", args=[self.shop.pk]))
        found = self.script(
            "var table = document.querySelector('table[data-table-label=\"identifiants\"]'),"
            "    box = table.closest('.table-scroll'), bar = box && box.previousElementSibling;"
            "return {boxed: !!box, bar: !!bar && bar.classList.contains('table-toolbar'),"
            "  inside: !!document.querySelector('.table-scroll .table-toolbar')};"
        )
        self.assertEqual(found, {"boxed": True, "bar": True, "inside": False})


@tag("browser")
class ListsAreCardsOnAPhoneInBrowserTests(PhoneBrowserTestCase):
    def setUp(self):
        super().setUp()
        from invoices.models import Invoice, ReceiptBatch

        cellar = make_supplier(code="CAVE_X", name="Cave Exemple des Vignerons", parser_key="")
        self.documents = []
        for day, number in ((12, "F-3"), (10, "F-1"), (11, "F-2")):
            document = make_invoice(
                supplier=cellar, invoice_number=number, invoice_date=date(2026, 3, day), parse_checks=CHECKED
            )
            make_invoice_line(
                invoice=document,
                product=make_product(supplier=cellar, raw_name=f"VIN ROUGE EXEMPLE CUVEE {number}"),
                quantity=6,
                total_ht="42.00",
            )
            self.documents.append(document)
        make_invoice(
            supplier=cellar,
            invoice_number="F-9",
            invoice_date=date(2026, 3, 13),
            status=Invoice.Status.ERROR,
            error_message="Lecture impossible : le fichier est illisible (inventé).",
        )
        self.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {
                    "name": "PXL_20260312_101112345_EXEMPLE_DE_NOM_DE_PHOTO.jpg",
                    "status": "ok",
                    "invoice_id": self.documents[0].pk,
                    "receipt": True,
                    "shop": "Cave Exemple des Vignerons",
                },
                {"name": "IMG_20260312_101113.jpg", "status": "duplicate", "message": "Déjà importé le 12/03/2026"},
            ],
        )
        wine = make_priced_stock_type(name="Vin rouge exemple", unit_cost_ht="7", quantity="10")
        for name in ("Verre de rouge exemple", "Kir au sureau exemple"):
            make_ingredient(make_recipe(name=name, selling_price_ttc="5.00"), stock_type=wine, quantity="0.12", group=0)

    def test_purchases_documents_are_cards_that_fit(self):
        self.as_a_phone(375)
        self.open(reverse("invoices:invoice_list"))
        self.assertEqual(
            self.cards(".documents-table"), {"table": "block", "head": "none", "row": "grid", "fits": True}
        )
        self.assertEqual(self.label_drawn(".documents-table tr.document-row td[data-label='État']"), '"État"')
        self.assertEqual(self.too_wide(375), "")

    def test_a_document_s_bulk_box_still_ticks(self):
        """Its box sits before the title, out of the grid: a real click
        reaches it, and the bulk bar counts it."""
        self.as_a_phone(375)
        self.open(reverse("invoices:invoice_list"))
        row = f"tr[data-document='{self.documents[0].pk}']"
        # Before the title, on its line - not a grid line of its own above it.
        self.assertEqual(
            self.script(
                "var row = document.querySelector(arguments[0]),"
                "    box = row.querySelector('td.select-col input').getBoundingClientRect(),"
                "    title = row.querySelector('td.select-col + td').getBoundingClientRect();"
                "return {before: box.right <= title.left + 0.5, sameLine: box.top < title.bottom && box.bottom > title.top};",
                row,
            ),
            {"before": True, "sameLine": True},
        )
        self.element(f"{row} input[data-bulk-item]").click()
        found = self.script(
            "var form = document.getElementById('invoice-bulk-form');"
            "return {count: form.querySelector('[data-bulk-count]').textContent,"
            "  enabled: !form.querySelector('[data-bulk-submit]').disabled,"
            "  opened: !!document.querySelector('tr.document-preview-row:not([hidden])')};"
        )
        # Ticked, and only ticked: the box is not a way into the row.
        self.assertEqual(found, {"count": "1 sélectionné", "enabled": True, "opened": False})

    def test_every_document_is_ticked_at_once_on_a_phone(self):
        """« Tout sélectionner » sat in the table's header row, which a phone
        does not draw once the rows are cards: under 860 px every document
        of an import had to be ticked one by one (review of 30/09). It is in
        the bulk bar now, drawn, and a real click on it ticks every card."""
        self.as_a_phone(375)
        self.open(reverse("invoices:invoice_list"))
        boxes = [
            box
            for box in self.driver.find_elements("css selector", "input[data-bulk-all][form='invoice-bulk-form']")
            if box.is_displayed()
        ]
        self.assertEqual(len(boxes), 1, "no « Tout sélectionner » drawn on a phone")
        boxes[0].click()
        found = self.script(
            "var form = document.getElementById('invoice-bulk-form');"
            "var items = Array.from(document.querySelectorAll(\"input[data-bulk-item][form='invoice-bulk-form']\"))"
            "  .filter(function (item) { return item.getClientRects().length > 0; });"
            "return {drawn: items.length, ticked: items.filter(function (item) { return item.checked; }).length,"
            "  count: form.querySelector('[data-bulk-count]').textContent,"
            "  enabled: !form.querySelector('[data-bulk-submit]').disabled};"
        )
        # The three documents and the unreadable one.
        self.assertGreaterEqual(found["drawn"], 4, found)
        self.assertEqual(
            found,
            {
                "drawn": found["drawn"],
                "ticked": found["drawn"],
                "count": f"{found['drawn']} sélectionnés",
                "enabled": True,
            },
        )

    def test_without_has_a_document_s_box_stays_in_its_card(self):
        """A card's bulk box is lifted out of its grid against its row, made
        its containing block by a rule under :has(). Where :has() is not
        supported that rule is dropped, and a box lifted out by a rule
        without it was placed against the page: every document's box at
        one spot, under the sticky bar, none reachable (review of 30/09).
        With every :has() rule dropped the box is an ordinary cell of its
        card - drawn inside it, and what a finger at its middle finds."""
        self.as_a_phone(375)
        self.open(reverse("invoices:invoice_list"))
        self.assertGreater(self.script(WITHOUT_HAS), 0, "no :has() rule dropped: nothing to prove")
        found = self.script(
            "return Array.from(document.querySelectorAll('.documents-table tr.document-row')).map(function (row) {"
            "  var box = row.querySelector('td.select-col input');"
            "  box.scrollIntoView({block: 'center'});"
            "  var r = box.getBoundingClientRect(), card = row.getBoundingClientRect();"
            "  var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            "  return {reached: hit === box, inCard: r.left >= card.left - 0.5 && r.right <= card.right + 0.5"
            "    && r.top >= card.top - 0.5 && r.bottom <= card.bottom + 0.5};"
            "});"
        )
        self.assertGreaterEqual(len(found), 4)
        self.assertEqual([row for row in found if not (row["reached"] and row["inCard"])], [], found)
        # Still cards: the header row is not drawn.
        self.assertEqual(self.cards(".documents-table")["head"], "none")

    def test_on_a_folding_phone_s_cover_a_document_keeps_its_first_line(self):
        """At 280 px (a folding phone's cover screen) a document's card, its
        bulk box's room taken off, fell to one grid track: « Vérifier » came
        first and its supplier's name under it, the box beside the button
        (review of 30/09, from 280 to 285 px). A row with an end cell keeps
        two tracks: box, supplier and button on one line."""
        for width in (280, 285):
            with self.subTest(width=width):
                self.as_a_phone(width)
                self.open(reverse("invoices:invoice_list"))
                rows = self.script(FIRST_LINE, ".documents-table tr.document-row")
                self.assertGreaterEqual(len(rows), 4)
                self.assertEqual([row for row in rows if row["problems"]], [])
                self.assertTrue(all(len(row["tracks"].split()) >= 2 for row in rows), rows)

    def test_a_long_state_or_till_name_stays_in_its_cell(self):
        """A state's pill and a till's name are nowrap (the wide table's),
        and inside a card they ran out of their cell: « Fournisseur à
        confirmer » (154 px) over the next figure at 430 px and past the card
        at 320, « Supprimé depuis l'import » past an import's card, a long
        till name out of its recipe's card and the recipes' box scrolling
        sideways (review of 30/09). The fixtures above only drew « Vérifié »
        and « Déjà importé »."""
        from invoices.models import Invoice, ReceiptBatch
        from recipes.models import PosProduct, Recipe

        cellar = self.documents[0].supplier
        doubted = make_invoice(
            supplier=cellar,
            invoice_number="F-4",
            invoice_date=date(2026, 3, 14),
            supplier_doubt="Imprime le n° SIREN d'un autre fournisseur (inventé).",
        )
        make_invoice(
            supplier=cellar, invoice_number="F-5", invoice_date=date(2026, 3, 15), status=Invoice.Status.NEEDS_REVIEW
        )
        gone = make_invoice(supplier=cellar, invoice_number="F-8")
        gone_pk = gone.pk
        gone.delete()
        batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {
                    "name": "IMG_20260314_090000.jpg",
                    "status": "ok",
                    "invoice_id": doubted.pk,
                    "receipt": True,
                    "shop": "Cave Exemple des Vignerons",
                },
                {
                    "name": "IMG_20260314_090001.jpg",
                    "status": "ok",
                    "invoice_id": gone_pk,
                    "receipt": True,
                    "shop": "Cave Exemple des Vignerons",
                },
                {
                    "name": "IMG_20260314_090002.jpg",
                    "status": "unrecognised",
                    "header": "EPICERIE INCONNUE EXEMPLE",
                    "read_date": "14/03/2026",
                    "read_total": "4,20",
                },
            ],
        )
        kir = Recipe.objects.get(name="Kir au sureau exemple")
        for name in (
            "KIR AU SUREAU EXEMPLE DE LA MAISON HAPPY HOUR GRAND VERRE",
            "SPRITZAPEROLEXEMPLEHAPPYHOURGRANDVERRE",
        ):
            PosProduct.objects.create(name=name, total_quantity=3, recipe=kir)

        lists = (
            (
                reverse("invoices:invoice_list"),
                ".documents-table",
                ".documents-table .status-pill",
                {"Fournisseur à confirmer", "Produits à classer"},
            ),
            (
                reverse("invoices:receipt_batch", args=[batch.pk]),
                "#receipt-batch-status table.phone-cards",
                "#receipt-batch-status table.phone-cards .status-pill",
                {"Fournisseur à confirmer", "Supprimé depuis l'import", "Enseigne inconnue"},
            ),
            (
                reverse("recipes:recipe_list"),
                "table[data-table-label='recettes']",
                "table[data-table-label='recettes'] .till-chip",
                {"KIR AU SUREAU EXEMPLE DE LA MAISON HAPPY HOUR GRAND VERRE", "SPRITZAPEROLEXEMPLEHAPPYHOURGRANDVERRE"},
            ),
        )
        for width in WIDTHS:
            self.as_a_phone(width)
            for path, table, css, long_ones in lists:
                with self.subTest(width=width, page=path):
                    self.open(path)
                    found = self.script(OUT_OF_ITS_CELL, css)
                    # Measured on the long ones, not only on « Vérifié ».
                    self.assertLessEqual(long_ones, set(found["drawn"]), found["drawn"])
                    self.assertEqual(found["out"], [])
                    self.assertTrue(self.cards(table)["fits"], f"{table}: its box scrolls sideways")
                    self.assertEqual(self.too_wide(width), "")

    def test_a_document_s_lines_keep_their_words_whole(self):
        """A document's lines open in a table under its card, and took the
        card's cells' `overflow-wrap: anywhere`: its columns shrank to a
        letter - « Ligne » on five lines at 320 px, a product's name one or
        two letters a line - instead of the table scrolling in its own box
        (review of 30/09). Every word of it stays on one line, and the page
        still fits the phone."""
        cellar = self.documents[0].supplier
        document = make_invoice(
            supplier=cellar, invoice_number="F-6", invoice_date=date(2026, 3, 16), parse_checks=CHECKED
        )
        for name in ("CHAMPAGNE BRUT RESERVE EXEMPLE 75CL", "PASTIS EXEMPLE 45D 1L"):
            make_invoice_line(
                invoice=document,
                product=make_product(supplier=cellar, raw_name=name),
                quantity=6,
                total_ht="123.45",
            )
        for width in (320, 375):
            with self.subTest(width=width):
                self.as_a_phone(width)
                self.open(reverse("invoices:invoice_list"))
                self.element(f"tr[data-document='{document.pk}'] td[data-label='Type']").click()
                lines = f"#preview-{document.pk} table.sub-table"
                self.wait_for(lambda css=lines: self.script("return !!document.querySelector(arguments[0]);", css))
                found = self.script(BROKEN_WORDS, lines)
                self.assertGreater(found["words"], 15, found)
                self.assertEqual(found["broken"], [])
                self.assertEqual(self.too_wide(width), "")

    def test_a_document_opens_its_lines_across_its_card(self):
        self.as_a_phone(375)
        self.open(reverse("invoices:invoice_list"))
        pk = self.documents[0].pk
        self.element(f"tr[data-document='{pk}'] td[data-label='Type']").click()
        self.wait_for(
            lambda: self.script(
                "return !!document.querySelector('#preview-' + arguments[0] + ' .document-preview');", pk
            )
        )
        found = self.script(
            "var preview = document.getElementById('preview-' + arguments[0]), cell = preview.cells[0],"
            "    table = preview.closest('table'), box = table.parentElement;"
            "return {table: table.getBoundingClientRect().width, row: preview.getBoundingClientRect().width,"
            "  cell: cell.getBoundingClientRect().width, boxed: !!cell.querySelector('.table-scroll > table.sub-table'),"
            "  fits: box.scrollWidth <= box.clientWidth};",
            pk,
        )
        self.assertTrue(found["boxed"], found)
        self.assertTrue(found["fits"], found)
        self.assertAlmostEqual(found["row"], found["table"], delta=1)
        self.assertAlmostEqual(found["cell"], found["row"], delta=1)
        self.assertEqual(self.too_wide(375), "")

    def test_documents_to_fix_are_cards_that_fit(self):
        self.as_a_phone(375)
        self.open(reverse("invoices:receipt_queue"))
        table = "table[data-table-label='documents à corriger']"
        self.assertEqual(self.cards(table), {"table": "block", "head": "none", "row": "grid", "fits": True})
        found = self.script(
            "var row = document.querySelector(arguments[0]).tBodies[0].rows[0], button = row.querySelector('.phone-card-end .btn');"
            "return {row: row.getBoundingClientRect().right, button: button.getBoundingClientRect().right};",
            table,
        )
        self.assertLessEqual(found["button"], found["row"] + 0.5)
        self.assertEqual(self.too_wide(375), "")

    def test_an_import_s_files_are_cards_that_fit(self):
        """A camera's file name is the card's title; a file already imported
        says so across its card."""
        self.as_a_phone(375)
        self.open(reverse("invoices:receipt_batch", args=[self.batch.pk]))
        table = "#receipt-batch-status table.phone-cards"
        self.assertEqual(self.cards(table), {"table": "block", "head": "none", "row": "grid", "fits": True})
        found = self.script(
            "var rows = document.querySelector(arguments[0]).tBodies[0].rows;"
            "function inside(row) { var style = getComputedStyle(row);"
            "  return row.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight); }"
            "return {inside: inside(rows[1]), message: rows[1].querySelector('td[colspan]').getBoundingClientRect().width,"
            "  titleRow: inside(rows[0]), title: rows[0].cells[0].getBoundingClientRect().width,"
            "  name: rows[0].cells[0].textContent.trim()};",
            table,
        )
        self.assertEqual(found["name"], "PXL_20260312_101112345_EXEMPLE_DE_NOM_DE_PHOTO.jpg")
        self.assertAlmostEqual(found["title"], found["titleRow"], delta=1)
        self.assertAlmostEqual(found["message"], found["inside"], delta=1)
        self.assertEqual(self.too_wide(375), "")

    def test_the_recipes_are_cards_and_still_searched(self):
        self.as_a_phone(375)
        self.open(reverse("recipes:recipe_list"))
        table = "table[data-table-label='recettes']"
        self.assertEqual(self.cards(table), {"table": "block", "head": "none", "row": "grid", "fits": True})
        self.assertEqual(self.label_drawn(f"{table} td[data-label='Coût (HT)']"), '"Coût (HT)"')
        self.element(".table-toolbar input[type=search]").send_keys("sureau")
        self.wait_for(
            lambda: self.script("return document.querySelector('.table-count').textContent;") == "1 / 2 recettes"
        )
        self.assertEqual(self.too_wide(375), "")

    def test_on_a_laptop_a_sort_button_shows_its_focus(self):
        """A column's sort button (datatable.js, `th.sortable > button`) is
        restyled with `all: unset`, which took the app's focus ring with it:
        reached by Tab, a table's header showed no focus at all (review of
        30/09; accounts/tests/test_topbar_browser.py walks the menu to
        « Se déconnecter », the other button it hid). From « Tout
        sélectionner » the next Tab is the first column's button - the bar's
        « Supprimer la sélection… » is disabled until a box is ticked."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        self.as_a_laptop()
        self.open(reverse("invoices:invoice_list"))
        self.script("document.querySelector(\"input[data-bulk-all][form='invoice-bulk-form']\").focus();")
        ActionChains(self.driver).send_keys(Keys.TAB).perform()
        found = self.script(
            "var e = document.activeElement, s = getComputedStyle(e);"
            "return {sort: e.matches('.documents-table th.sortable > button'), ring: e.matches(':focus-visible'),"
            "  outline: s.outlineStyle + ' ' + s.outlineWidth, words: e.textContent.trim()};"
        )
        self.assertEqual(found, {"sort": True, "ring": True, "outline": "solid 2px", "words": "Fournisseur"})

    def test_on_a_laptop_they_are_still_tables(self):
        """With a mouse at 1280 px nothing of the cards applies: the header
        row, the rows, no label drawn over a figure - as on 29/09."""
        self.as_a_laptop()
        for path, table in (
            (reverse("invoices:invoice_list"), ".documents-table"),
            (reverse("invoices:receipt_queue"), "table[data-table-label='documents à corriger']"),
            (reverse("invoices:receipt_batch", args=[self.batch.pk]), "#receipt-batch-status table"),
            (reverse("recipes:recipe_list"), "table[data-table-label='recettes']"),
        ):
            with self.subTest(page=path):
                self.open(path)
                self.assertEqual(
                    self.script(
                        "var table = document.querySelector(arguments[0]), row = table.tBodies[0].rows[0],"
                        "    labelled = row.querySelector('td[data-label]');"
                        "return [getComputedStyle(table).display, getComputedStyle(table.tHead).display,"
                        "  getComputedStyle(row).display, labelled ? getComputedStyle(labelled, '::before').content : 'none'];",
                        table,
                    ),
                    ["table", "table-header-group", "table-row", "none"],
                )
