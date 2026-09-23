"""Le panneau d'une ligne dit la même chose que la ligne qu'il explique.

« Du … au … » now narrows what a row of « Produits & charges » opens, so the
panel and the row hold exactly the same purchases and the same documents.
That is precisely what makes every remaining disagreement between them
visible - and a panel arguing with the figure above it is the
silently-wrong-money shape this codebase exists to catch. What this file
pins down, each from a review that reproduced it:

* **the money is the same money.** A charge's row rounds each line to the
  cent before adding it up (`charge_suppliers`); the panel added them raw,
  so twelve bills printed « 35,99 € » each over a foot reading 431,86 €
  while the row said 431,88 €. And an article's row worked its « Total TTC »
  out of HT while the panel printed `InvoiceLine.total_ttc` - a facturette
  printing 39,99 € came out at 40,00 € on the row and 39,99 € in the panel
  just below it. One formula on both sides, or the page holds two answers to
  one question;
* **what is not a purchase is not called one.** The row's « Acheté » counts
  `MovementKind.PURCHASE` alone. The panel lists every movement, which was
  merely loose until the window gave it a header saying « Achats » - over a
  broken bottle. It names what it holds, and each such line says which kind
  it is;
* **the rows are ordered by the date the panel prints.** The column shows
  `effective_date` now; ordered by the invoice's date, a delivery invoiced
  in January and received on the 27th sorted between the 3rd and the 14th,
  which reads as a sorting bug;
* **« tout l'historique » is a door, not a trapdoor.** It dropped the two
  dates from the URL and nothing on screen offered them back - and the row
  is only fetched once (`stock_list.html` marks it loaded), so the panel
  went on saying « tout » under « Acheté 12 » until the whole page was
  reloaded. The link carries the window back with it (`tout=1`), and the
  all-history panel offers the dates.

Data invented.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse

from inventory.models import MovementKind, UnitChoices
from inventory.tests.test_panel_window import ArticlePanelTestCase
from inventory.tests.test_purchase_window import FEBRUARY, PurchaseWindowTestCase
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_type,
    make_supplier,
)

D = Decimal


class AChargePanelFootsWhatItsOwnLinesPrintTests(PurchaseWindowTestCase):
    """A subscription of 29,99 € HT at 20 % is 35,988 € - printed 35,99 € on
    every line of the panel, and counted 35,99 € on the row above it, which
    rounds before it adds. Added raw, twelve of them foot 431,86 € under
    twelve visible lines of 35,99 €: the panel contradicts its own column
    and the row it explains, by two cents."""

    def setUp(self):
        super().setUp()
        self.fournisseur = make_supplier(
            code="ABO_X", name="Abonnement Exemple", parser_key="", expenses_only=True
        )
        self.abonnement = make_product(
            supplier=self.fournisseur, raw_name="ABONNEMENT", is_expense=True
        )
        for month in range(1, 13):
            bill = make_invoice(
                supplier=self.fournisseur,
                invoice_date=date(2026, month, 5),
                invoice_number=f"ABO-2026-{month:02d}",
            )
            make_invoice_line(
                invoice=bill, product=self.abonnement, total_ht="29.99", vat_rate=D("0.20")
            )
        self.year = {"du": "2026-01-01", "au": "2026-12-31"}

    def supplier_url(self):
        return reverse("inventory:charge_supplier_documents", args=[self.fournisseur.pk])

    def test_the_foot_is_the_sum_of_the_lines_the_panel_prints(self):
        panel = self.client.get(self.supplier_url(), self.year)
        printed = sum((row["total_ttc"] for row in panel.context["rows"]), D("0"))
        self.assertEqual(printed, D("431.88"))
        self.assertEqual(panel.context["total_ttc"], D("431.88"))

    def test_and_it_is_the_total_of_the_row_it_hangs_under(self):
        (row,) = self.page(**self.year).context["charge_suppliers"]
        panel = self.client.get(self.supplier_url(), self.year)
        self.assertEqual(panel.context["total_ttc"], row["total_ttc"])

    def test_a_document_of_several_lines_adds_up_the_same_way(self):
        """Three lines of 33,33 € HT at 20 % on one bill: the row counts
        three times 40,00 €, and the panel's own row has to say 120,00 €."""
        bill = make_invoice(
            supplier=self.fournisseur, invoice_date=date(2026, 6, 20), invoice_number="ABO-TROIS"
        )
        for poste in ("POSTE A", "POSTE B", "POSTE C"):
            product = make_product(supplier=self.fournisseur, raw_name=poste, is_expense=True)
            make_invoice_line(
                invoice=bill, product=product, raw_name=poste, total_ht="33.33", vat_rate=D("0.20")
            )
        (row,) = self.page(du="2026-06-01", au="2026-06-30").context["charge_suppliers"]
        panel = self.client.get(self.supplier_url(), {"du": "2026-06-01", "au": "2026-06-30"})
        (document,) = [entry for entry in panel.context["rows"] if entry["invoice"].pk == bill.pk]
        self.assertEqual(document["total_ttc"], D("120.00"))
        self.assertEqual(panel.context["total_ttc"], row["total_ttc"])


class AnArticleRowCountsWhatItsPurchasePrintedTests(PurchaseWindowTestCase):
    """A facturette prints 39,99 € where 33,33 € HT at 20 % works out to
    39,996 €, and 39,99 € is what left the bank (CLAUDE.md, « a receipt line
    keeps its printed TTC »). The panel reads `InvoiceLine.total_ttc`, which
    prefers that printed amount; the row's scan worked its own out of HT. One
    purchase in the window, and the page held two answers - 40,00 € above
    39,99 €."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        product = self.product_for(self.vodka)
        invoice = make_invoice(supplier=self.supplier, invoice_date=date(2026, 2, 10))
        self.line = make_invoice_line(
            invoice=invoice, product=product, quantity=1,
            total_ht="33.33", vat_rate=D("0.20"), printed_ttc=D("39.99"),
        )
        make_movement(
            stock_type=self.vodka, quantity="1", unit_cost_ht="33.33", invoice_line=self.line
        )

    def panel(self, **parameters):
        return self.client.get(
            reverse("inventory:stock_type_movements", args=[self.vodka.pk]), parameters
        )

    def test_the_row_prints_the_amount_the_panel_prints(self):
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        (entry,) = self.panel(**FEBRUARY).context["movements"]
        self.assertEqual(entry["total_ttc"], D("39.99"))
        self.assertEqual(row["value_ttc"], D("39.99"))

    def test_a_promotion_comes_off_both_sides(self):
        """The share of a « 3 pour 2 » sits beside the printed price
        (`discount_ttc`), and `InvoiceLine.total_ttc` takes it off."""
        self.line.discount_ttc = D("2.00")
        self.line.save(update_fields=["discount_ttc"])
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        (entry,) = self.panel(**FEBRUARY).context["movements"]
        self.assertEqual(entry["total_ttc"], D("37.99"))
        self.assertEqual(row["value_ttc"], D("37.99"))

    def test_a_line_that_printed_nothing_is_still_worked_out_from_ht(self):
        """A supplier's invoice prints HT, and nothing changes for it - the
        printed amount is preferred where there is one, not invented."""
        rhum = make_stock_type(name="Rhum", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(rhum, date(2026, 2, 12), quantity="1", total_ht="100")
        row = self.rows(self.page(**FEBRUARY))["Rhum"]
        self.assertEqual(row["value_ttc"], D("120"))


class AnArticleRowCountsTheHtItsLinesPrintTests(PurchaseWindowTestCase):
    """The same disagreement, one column to the left.

    `unit_cost_ht` is the line's amount DIVIDED by the quantity, stored to
    four decimals: 2 000 touillettes charged 28,84 € are priced 0,01442,
    kept 0,0144, and multiplied back they make 28,80 €. So the row read
    28,80 € over a panel listing 7,21 € and 21,63 €, and the page's own
    « Total acheté (HT) » came out a few centimes under what the invoices
    charge (a handful of articles, measured on a copy of the real database,
    20/09; the amounts here are invented).

    The document's amount wins, as it does in TTC. What has no line at all -
    a correction typed by hand - still has the ledger's arithmetic and
    nothing else, and keeps it.
    """

    def setUp(self):
        super().setUp()
        self.touillettes = make_stock_type(name="Touillettes", unit=UnitChoices.UNIT, category="Consommables")
        product = self.product_for(self.touillettes)
        for day, count, amount in (
            (date(2026, 2, 5), 500, "7.21"),
            (date(2026, 2, 16), 1500, "21.63"),
        ):
            invoice = make_invoice(supplier=self.supplier, invoice_date=day)
            line = make_invoice_line(
                invoice=invoice, product=product, quantity=count,
                total_ht=amount, vat_rate=D("0.20"),
            )
            # What the import books: the amount divided by the count, and
            # the field keeps four decimals of it.
            make_movement(
                stock_type=self.touillettes, quantity=str(count),
                unit_cost_ht=(D(amount) / count).quantize(D("0.0001")), invoice_line=line,
            )

    def panel_lines(self, **parameters):
        answer = self.client.get(
            reverse("inventory:stock_type_movements", args=[self.touillettes.pk]), parameters
        )
        return [entry["line"] for entry in answer.context["movements"] if entry["line"]]

    def test_the_row_prints_what_its_lines_charge(self):
        row = self.rows(self.page(**FEBRUARY))["Touillettes"]
        lines = self.panel_lines(**FEBRUARY)
        self.assertEqual(sum((line.total_ht for line in lines), D("0")), D("28.84"))
        self.assertEqual(row["value_ht"], D("28.84"))

    def test_the_page_total_is_what_the_invoices_charge(self):
        self.assertEqual(self.page(**FEBRUARY).context["total_value_ht"], D("28.84"))

    def test_all_time_too(self):
        """Not a property of the window: the same two deliveries add up to
        the same 28,84 € with no dates asked at all."""
        self.assertEqual(self.rows(self.page())["Touillettes"]["value_ht"], D("28.84"))

    def test_a_movement_with_no_line_keeps_the_ledgers_own_arithmetic(self):
        """A correction typed by hand has no document to quote."""
        sirop = make_stock_type(name="Sirop", unit=UnitChoices.LITRE, category="Softs")
        make_movement(
            stock_type=sirop, quantity="2", unit_cost_ht="3.5000",
            occurred_on=date(2026, 2, 10), note="Correction manuelle",
        )
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Sirop"]["value_ht"], D("7.0000"))

    def test_the_unit_price_is_still_the_ledgers(self):
        """`value_ht_by_type` feeds the average unit cost, the way
        StockType.current_unit_cost_ht computes it, and must go on saying
        what the ledger holds rather than what the document charged."""
        self.assertEqual(
            self.touillettes.current_unit_cost_ht,
            (D("500") * D("0.0144") + D("1500") * D("0.0144")) / D("2000"),
        )


class WhatIsNotAnAchatSaysSoTests(ArticlePanelTestCase):
    """The row's « Acheté » counts purchases alone. The panel lists every
    movement of the window - a broken bottle, a ledger correction - and the
    window is what makes that visible: forty rows of history hid one loss,
    two rows do not. The header is now a claim in French about what is under
    it, so it names what it holds and every such line says what it is."""

    def setUp(self):
        super().setUp()
        make_movement(
            stock_type=self.vodka, quantity="-2", unit_cost_ht="20", kind=MovementKind.LOSS,
            occurred_on=date(2026, 2, 15), note="Bouteille cassée",
        )

    def test_the_header_does_not_call_a_loss_a_purchase(self):
        panel = self.panel(**FEBRUARY)
        self.assertContains(panel, "Achats et autres mouvements du 01/02/2026 au 28/02/2026")

    def test_the_line_says_which_kind_it_is(self):
        self.assertContains(self.panel(**FEBRUARY), "Perte connue")

    def test_a_window_of_purchases_alone_is_still_headed_achats(self):
        """« et autres mouvements » over a page of deliveries would be a
        second lie, the other way round."""
        panel = self.panel(du="2026-02-01", au="2026-02-14")
        self.assertContains(panel, "Achats du 01/02/2026 au 14/02/2026")
        self.assertNotContains(panel, "autres mouvements")

    def test_a_purchase_is_not_labelled_at_all(self):
        """Labelling every line « Achat » would make the exception invisible
        again - the column exists to point at the line that is not one."""
        panel = self.panel(du="2026-02-01", au="2026-02-14")
        self.assertNotContains(panel, "(Achat)")


class ThePanelIsOrderedByTheDateItPrintsTests(ArticlePanelTestCase):
    """Ordered by the invoice's date while the column prints
    `effective_date`, a delivery invoiced on 20/01 and received on 27/02 sat
    below the 3rd - and a reader looking at a dated column out of order reads
    a bug."""

    def test_the_dates_come_down_the_column_in_order(self):
        self.buy(self.vodka, date(2026, 2, 3), quantity="1", total_ht="10")
        self.buy(self.vodka, date(2026, 2, 25), quantity="1", total_ht="10")
        make_movement(
            stock_type=self.vodka, quantity="-1", unit_cost_ht="10",
            occurred_on=date(2026, 2, 14), note="Correction",
        )
        late = self.buy(self.vodka, date(2026, 1, 20), quantity="1", total_ht="10")
        late.occurred_on = date(2026, 2, 27)
        late.save(update_fields=["occurred_on"])
        dates = [entry["date"] for entry in self.panel(**FEBRUARY).context["movements"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_the_whole_history_is_ordered_too(self):
        dates = [entry["date"] for entry in self.panel().context["movements"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


class TheWayBackToTheDatesTests(ArticlePanelTestCase):
    """« tout l'historique » widened the panel and threw the window away:
    the row is fetched once and marked loaded, so nothing put February back
    until the page was reloaded whole - « Acheté 12 » over the forty
    purchases this change exists to stop showing."""

    def test_the_link_carries_the_window_with_it(self):
        panel = self.panel(**FEBRUARY)
        self.assertIn("du=2026-02-01", panel.context["all_url"])
        self.assertIn("au=2026-02-28", panel.context["all_url"])
        self.assertIn("tout=1", panel.context["all_url"])

    def test_and_what_it_opens_really_is_the_whole_history(self):
        whole = self.client.get(self.panel(**FEBRUARY).context["all_url"])
        self.assertContains(whole, "10/01/2026")
        self.assertContains(whole, "10/02/2026")

    def test_the_whole_history_offers_the_dates_back(self):
        whole = self.client.get(self.panel(**FEBRUARY).context["all_url"])
        self.assertContains(whole, "revenir du 01/02/2026 au 28/02/2026")
        self.assertContains(whole, 'hx-get="%s?du=2026-02-01&amp;au=2026-02-28"' % self.panel_url())

    def test_the_way_back_gives_the_window_back(self):
        whole = self.client.get(self.panel(**FEBRUARY).context["all_url"])
        again = self.client.get(whole.context["back_url"])
        self.assertContains(again, "10/02/2026")
        self.assertNotContains(again, "10/01/2026")

    def test_a_panel_nobody_asked_dates_of_offers_nothing_back(self):
        panel = self.panel()
        self.assertIsNone(panel.context["back_url"])
        self.assertNotContains(panel, "revenir")

    def test_the_door_is_typed_in_a_url_like_everything_else(self):
        """`tout=1` arrives in a query string - on its own, or beside a date
        that is no date - and neither is a 500 nor a way back to nowhere."""
        alone = self.panel(tout="1")
        self.assertEqual(alone.status_code, 200)
        self.assertIsNone(alone.context["back_url"])
        nonsense = self.panel(du="hier", tout="1")
        self.assertEqual(nonsense.status_code, 200)
        self.assertIsNone(nonsense.context["back_url"])
        self.assertContains(nonsense, "10/01/2026")

    def test_half_a_window_is_offered_back_as_half_a_window(self):
        whole = self.client.get(self.panel(du="2026-02-01").context["all_url"])
        self.assertContains(whole, "revenir depuis le 01/02/2026")

    def test_an_empty_window_has_the_same_door(self):
        panel = self.panel(du="2026-06-01", au="2026-06-30")
        whole = self.client.get(panel.context["all_url"])
        self.assertContains(whole, "revenir du 01/06/2026 au 30/06/2026")


class AChargesWayBackTests(PurchaseWindowTestCase):
    """The same door on the charges fold, whose link sits in the foot of the
    table rather than above it."""

    def setUp(self):
        super().setUp()
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        for month in (1, 2, 3):
            bill = make_invoice(
                supplier=self.bailleur,
                invoice_date=date(2026, month, 5),
                invoice_number=f"LOYER-2026-{month:02d}",
            )
            make_invoice_line(invoice=bill, product=self.loyer, total_ht="500", vat_rate=D("0.20"))

    def supplier_url(self):
        return reverse("inventory:charge_supplier_documents", args=[self.bailleur.pk])

    def test_the_foots_link_carries_the_window(self):
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertIn("tout=1", panel.context["all_url"])
        self.assertIn("du=2026-02-01", panel.context["all_url"])

    def test_the_whole_history_offers_the_dates_back(self):
        whole = self.client.get(self.client.get(self.supplier_url(), FEBRUARY).context["all_url"])
        self.assertContains(whole, "LOYER-2026-01")
        self.assertContains(whole, "revenir du 01/02/2026 au 28/02/2026")

    def test_the_way_back_gives_the_window_back(self):
        whole = self.client.get(self.client.get(self.supplier_url(), FEBRUARY).context["all_url"])
        again = self.client.get(whole.context["back_url"])
        self.assertContains(again, "LOYER-2026-02")
        self.assertNotContains(again, "LOYER-2026-01")

    def test_a_panel_nobody_asked_dates_of_still_says_all_history(self):
        panel = self.client.get(self.supplier_url())
        self.assertIsNone(panel.context["back_url"])
        self.assertContains(panel, "3 documents — tout l'historique")


class TheDocumentsColumnNamesTheWindowItCountsTests(PurchaseWindowTestCase):
    """« période » is this page's word for two physical counts. Over « du …
    au … » the column counted the dates and called them a période all the
    same, which is the one thing the header is there to say."""

    def setUp(self):
        super().setUp()
        supplier = make_supplier(
            code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True
        )
        product = make_product(supplier=supplier, raw_name="EAU", is_expense=True)
        bill = make_invoice(supplier=supplier, invoice_date=date(2026, 2, 5))
        make_invoice_line(invoice=bill, product=product, total_ht="40", vat_rate=D("0.055"))

    def test_under_two_dates_it_names_the_dates(self):
        response = self.page(**FEBRUARY)
        self.assertContains(response, "Documents (ces dates)")
        self.assertNotContains(response, "Documents (période)")
        self.assertNotContains(response, "Documents (12 mois)")

    def test_with_nothing_asked_it_is_still_the_twelve_months(self):
        self.assertContains(self.page(), "Documents (12 mois)")
