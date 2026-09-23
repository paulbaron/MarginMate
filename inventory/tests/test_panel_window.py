"""Ce qu'une ligne de « Produits & charges » ouvre suit les deux dates.

The owner, 20/09: « filtre aussi les articles avec les dates quand on
déroule les articles de la page Produits & charges ».

« Du … au … » already narrowed the list - the articles bought between the
dates, their « Acheté », leurs totaux, les charges en dessous. Opening a row
still answered with the whole history, so the panel contradicted the very
row it explains: « Acheté 12 » on the row, quarante achats dans le panneau
juste en dessous. What this file pins down:

* an article's panel holds the purchases of the window and no others,
  chosen on `StockMovement.effective_date` - the date `catalogue_context`
  summed the row's own figures by. Chosen on the invoice's date instead,
  the panel would not add up to the row above it, which is the
  silently-wrong-money class this codebase exists to avoid;
* a charge's panel - a supplier's, a poste's - does the same on
  `invoice_date`, which is what `charge_suppliers` windows its rows by,
  documents filed with nothing read included: they count on the row, so
  they follow the row;
* the panels are fetched with the window in their URL, built once in the
  view, so no attribute can be the one that forgets it;
* a windowed panel says which dates it holds and offers « tout
  l'historique » beside them - and an empty one says « rien entre ces
  dates », never « jamais acheté », with the way back on screen;
* the « Date » column prints the date that put the row in the window: a
  manual correction dated by `occurred_on` was shown as « — », which reads
  as a bug once a window has selected it;
* the 📈 curves are deliberately NOT filtered - narrowed to one month a
  curve is two points and « pas assez d'historique », a feature removed
  rather than a window applied - so their URLs stay bare;
* and a chosen `?inventaire=` keeps its documented all-history panels: it
  is two physical counts with its own arithmetic, and the page says so.

Data invented.
"""

from datetime import date, datetime
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventory.models import StockMovement, UnitChoices
from inventory.tests.test_purchase_window import FEBRUARY, PurchaseWindowTestCase
from invoices.models import Invoice
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_movement,
    make_product,
    make_stock_take,
    make_stock_type,
    make_supplier,
)

D = Decimal


class ArticlePanelTestCase(PurchaseWindowTestCase):
    """One article bought twice: in January and in February."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")

    def panel_url(self, stock_type=None):
        return reverse("inventory:stock_type_movements", args=[(stock_type or self.vodka).pk])

    def panel(self, stock_type=None, **parameters):
        return self.client.get(self.panel_url(stock_type), parameters)


class AnArticlePanelHoldsTheWindowTests(ArticlePanelTestCase):
    def test_only_the_purchases_of_the_window(self):
        panel = self.panel(**FEBRUARY)
        self.assertContains(panel, "10/02/2026")
        self.assertNotContains(panel, "10/01/2026")

    def test_the_panel_adds_up_to_the_row_above_it(self):
        """The row says « Acheté 12 » and « 240,00 € HT » over the same
        window; the panel has to say the same, or it argues with the figure
        it was opened to explain."""
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["value_ht"], D("240"))
        panel = self.panel(**FEBRUARY)
        self.assertEqual(len(panel.context["movements"]), 1)
        self.assertContains(panel, "240.00")
        self.assertNotContains(panel, "120.00")

    def test_without_a_window_it_is_the_whole_history_as_before(self):
        panel = self.panel()
        self.assertContains(panel, "10/02/2026")
        self.assertContains(panel, "10/01/2026")

    def test_both_ends_are_included(self):
        """« au 28 » means the 28th, exactly as the list reads it.

        Asserted on the dates the window SELECTED, not on the two strings
        found in the page: the windowed header prints « du 01/02/2026 au
        28/02/2026 » itself, so assertContains passed over an empty panel."""
        self.buy(self.vodka, date(2026, 2, 1), quantity="1", total_ht="11")
        self.buy(self.vodka, date(2026, 2, 28), quantity="1", total_ht="22")
        chosen = {entry["date"] for entry in self.panel(**FEBRUARY).context["movements"]}
        self.assertEqual(chosen, {date(2026, 2, 1), date(2026, 2, 10), date(2026, 2, 28)})

    def test_the_days_just_outside_are_out(self):
        self.buy(self.vodka, date(2026, 1, 31), quantity="1", total_ht="33")
        self.buy(self.vodka, date(2026, 3, 1), quantity="1", total_ht="44")
        panel = self.panel(**FEBRUARY)
        self.assertNotContains(panel, "31/01/2026")
        self.assertNotContains(panel, "01/03/2026")

    def test_one_end_alone_is_a_window(self):
        self.assertNotContains(self.panel(du="2026-02-01"), "10/01/2026")
        self.assertNotContains(self.panel(au="2026-01-31"), "10/02/2026")

    def test_a_date_that_is_no_date_is_the_whole_history(self):
        """A panel URL is a query string like any other - a stale bookmark
        and a hand-typed address both land here, and neither is a 500."""
        panel = self.panel(du="n-importe-quoi", au="2026-02-30")
        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, "10/01/2026")
        self.assertContains(panel, "10/02/2026")


class ItIsTheDateTheRowWasSummedByTests(ArticlePanelTestCase):
    """`effective_date` and not the invoice's date: `occurred_on` first (a
    delivery received a month after it was invoiced), then the invoice's own
    date, then when the movement was typed in. That is the order
    `catalogue_context` scans by, and a panel disagreeing with it is a panel
    that does not add up to its row."""

    def test_occurred_on_beats_the_invoice_date(self):
        movement = self.buy(self.vodka, date(2026, 1, 20), quantity="5", total_ht="100")
        movement.occurred_on = date(2026, 2, 14)
        movement.save(update_fields=["occurred_on"])
        # The row counts it in February - seventeen litres, not twelve - so
        # the panel has to hold it too.
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("17"))
        panel = self.panel(**FEBRUARY)
        self.assertEqual(len(panel.context["movements"]), 2)
        self.assertContains(panel, "100.00")

    def test_a_manual_correction_is_dated_by_occurred_on(self):
        """No invoice at all - a stock correction typed by hand."""
        make_movement(
            stock_type=self.vodka, quantity="-2", unit_cost_ht="20",
            occurred_on=date(2026, 2, 14), note="Casse",
        )
        self.assertEqual(len(self.panel(**FEBRUARY).context["movements"]), 2)
        self.assertEqual(len(self.panel(du="2026-03-01").context["movements"]), 0)

    def test_reading_the_date_per_movement_costs_no_query(self):
        """The catalogue's scan reads the three date columns by hand because
        it walks every movement there is; here the property is read straight
        off the model, which is only free while the invoice comes with it.
        Drop the select_related and a long history is a query a line - so
        the count must not follow the number of rows."""
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.panel_url(), FEBRUARY)
        for day in (2, 3, 4, 5, 6, 7):
            self.buy(self.vodka, date(2026, 2, day), quantity="1", total_ht="10")
        with CaptureQueriesContext(connection) as many:
            self.client.get(self.panel_url(), FEBRUARY)
        self.assertEqual(len(many), len(few))

    def test_a_movement_dated_by_neither_falls_back_on_when_it_was_typed(self):
        movement = make_movement(stock_type=self.vodka, quantity="7", unit_cost_ht="20")
        # auto_now_add cannot be assigned, and a plain UPDATE is the one way
        # to give a row a creation date a test chooses.
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime(2026, 2, 14, 12, 0))
        )
        self.assertEqual(len(self.panel(**FEBRUARY).context["movements"]), 2)


class TheDateShownIsTheDateThatSelectedItTests(ArticlePanelTestCase):
    """« Date » printed the invoice's date, and « — » where there was none.
    Selected by a window and shown as « — », a row reads as a bug: the
    column prints the date the movement actually counts on."""

    def test_a_manual_correction_prints_its_own_date(self):
        make_movement(
            stock_type=self.vodka, quantity="-2", unit_cost_ht="20",
            occurred_on=date(2026, 2, 14), note="Casse",
        )
        self.assertContains(self.panel(**FEBRUARY), "14/02/2026")

    def test_a_delivery_received_later_prints_the_day_it_arrived(self):
        movement = self.buy(self.vodka, date(2026, 1, 20), quantity="5", total_ht="100")
        movement.occurred_on = date(2026, 2, 14)
        movement.save(update_fields=["occurred_on"])
        panel = self.panel(**FEBRUARY)
        self.assertContains(panel, "14/02/2026")
        self.assertNotContains(panel, "20/01/2026")

    def test_a_movement_with_no_invoice_prints_the_day_it_was_typed(self):
        """`created_at` is the last fallback and is never null, so a row a
        window selected always has a date to show for itself."""
        movement = make_movement(stock_type=self.vodka, quantity="3", unit_cost_ht="20")
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime(2026, 2, 18, 12, 0))
        )
        self.assertContains(self.panel(**FEBRUARY), "18/02/2026")


class AWindowedPanelSaysSoTests(ArticlePanelTestCase):
    """A panel showing part of a history has to say which part, and the way
    back to the whole of it has to be on screen - otherwise « Acheté 12 »
    over a single line reads as a history that lost itself."""

    def test_it_names_the_window_and_offers_the_whole_history(self):
        panel = self.panel(**FEBRUARY)
        self.assertContains(panel, "Achats du 01/02/2026 au 28/02/2026")
        self.assertContains(panel, "tout l'historique")
        # The link keeps the dates so the widened panel can offer them back
        # (see test_panel_matches_its_row): `tout=1` is « everything, and
        # remember what was asked ».
        self.assertContains(panel, 'hx-get="%s?du=2026-02-01&amp;au=2026-02-28&amp;tout=1"' % self.panel_url())

    def test_nothing_is_said_without_a_window(self):
        panel = self.panel()
        self.assertNotContains(panel, "Achats du 01/02/2026")
        self.assertNotContains(panel, "tout l'historique")

    def test_an_empty_window_says_rien_entre_ces_dates(self):
        """« Aucun achat enregistré pour le moment » says this article was
        never bought. Between two dates the truth is that nothing was bought
        between them, and the way out has to be clickable."""
        panel = self.panel(du="2026-06-01", au="2026-06-30")
        self.assertContains(panel, "Aucun achat du 01/06/2026 au 30/06/2026")
        self.assertNotContains(panel, "Aucun achat enregistré pour le moment")
        self.assertContains(panel, 'hx-get="%s?du=2026-06-01&amp;au=2026-06-30&amp;tout=1"' % self.panel_url())

    def test_an_article_never_bought_still_says_so(self):
        absinthe = make_stock_type(name="Absinthe", unit=UnitChoices.LITRE, category="Spiritueux")
        self.assertContains(self.panel(absinthe), "Aucun achat enregistré pour le moment")

    def test_the_link_replaces_the_whole_panel(self):
        """`closest td` is the wrong target: the charge panel's own link
        sits in the foot of a nested table, whose cell is the nearest <td>,
        and the panel would be swapped inside itself. Both fragments carry
        one wrapper instead, which a link can name from anywhere in them."""
        panel = self.panel(**FEBRUARY)
        self.assertContains(panel, 'class="row-panel"')
        self.assertContains(panel, 'hx-target="closest .row-panel" hx-swap="outerHTML"')
        self.assertNotContains(panel, 'hx-target="closest td"')


class ChargePanelsFollowTheWindowTests(PurchaseWindowTestCase):
    """A charge opens like a stock item, so it follows the same rule - on
    `invoice_date`, which is what `charge_suppliers` windows its own rows
    by."""

    def setUp(self):
        super().setUp()
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        for month, day in ((1, date(2026, 1, 5)), (2, date(2026, 2, 5)), (3, date(2026, 3, 5))):
            bill = make_invoice(
                supplier=self.bailleur, invoice_date=day, invoice_number=f"LOYER-2026-{month:02d}"
            )
            make_invoice_line(invoice=bill, product=self.loyer, total_ht="500", vat_rate=D("0.20"))

    def supplier_url(self):
        return reverse("inventory:charge_supplier_documents", args=[self.bailleur.pk])

    def poste_url(self):
        return reverse("inventory:charge_documents", args=[self.loyer.pk])

    def test_the_supplier_panel_holds_the_documents_of_the_window(self):
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertContains(panel, "LOYER-2026-02")
        self.assertNotContains(panel, "LOYER-2026-01")
        self.assertNotContains(panel, "LOYER-2026-03")

    def test_it_adds_up_to_its_own_row(self):
        (row,) = self.page(**FEBRUARY).context["charge_suppliers"]
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertEqual(len(panel.context["rows"]), row["documents"])
        self.assertEqual(panel.context["total_ttc"], row["total_ttc"])

    def test_a_poste_panel_does_the_same(self):
        panel = self.client.get(self.poste_url(), FEBRUARY)
        self.assertContains(panel, "LOYER-2026-02")
        self.assertNotContains(panel, "LOYER-2026-01")

    def test_without_a_window_both_are_the_whole_history(self):
        for url in (self.supplier_url(), self.poste_url()):
            with self.subTest(url=url):
                panel = self.client.get(url)
                self.assertContains(panel, "LOYER-2026-01")
                self.assertContains(panel, "LOYER-2026-03")

    def test_a_document_filed_with_nothing_read_follows_the_row(self):
        """It counts on the supplier's row - and it is the one that most
        needs its « Corriger », so it has to be reachable in the window that
        counted it, and absent from the ones that did not."""
        empty = make_invoice(
            supplier=self.bailleur, invoice_date=date(2026, 2, 20), invoice_number="AVIS-SANS-LIGNE"
        )
        (row,) = self.page(**FEBRUARY).context["charge_suppliers"]
        self.assertEqual(row["documents"], 2)
        self.assertContains(self.client.get(self.supplier_url(), FEBRUARY), empty.invoice_number)
        self.assertNotContains(
            self.client.get(self.supplier_url(), {"du": "2026-03-01"}), empty.invoice_number
        )

    def test_an_undated_document_is_in_no_window(self):
        """« Sans date » is where those are looked at, and the row does not
        count it either - charge_suppliers excludes them outright."""
        undated = make_invoice(
            supplier=self.bailleur, invoice_date=date(2026, 2, 20), invoice_number="AVIS-SANS-DATE"
        )
        make_invoice_line(invoice=undated, product=self.loyer, total_ht="90", vat_rate=D("0.20"))
        Invoice.objects.filter(pk=undated.pk).update(invoice_date=None)
        self.assertContains(self.client.get(self.supplier_url()), "AVIS-SANS-DATE")
        self.assertNotContains(self.client.get(self.supplier_url(), FEBRUARY), "AVIS-SANS-DATE")

    def test_the_foot_names_the_window_instead_of_promising_everything(self):
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertContains(panel, "1 document du 01/02/2026 au 28/02/2026")
        self.assertContains(
            panel, 'hx-get="%s?du=2026-02-01&amp;au=2026-02-28&amp;tout=1"' % self.supplier_url()
        )

    def test_the_foot_still_says_all_history_without_a_window(self):
        self.assertContains(self.client.get(self.supplier_url()), "3 documents — tout l'historique")

    def test_an_empty_window_says_rien_entre_ces_dates(self):
        """Naming the window proves nothing on its own - the foot of a FULL
        panel names it too, so this used to pass with the filter taken out.
        What says the filter ran is that no document is left."""
        panel = self.client.get(self.supplier_url(), {"du": "2026-06-01", "au": "2026-06-30"})
        self.assertEqual(panel.context["rows"], [])
        self.assertContains(panel, "Aucun document pour « Bailleur Exemple » du 01/06/2026 au 30/06/2026")
        self.assertNotContains(panel, "LOYER-2026-02")
        self.assertContains(
            panel, 'hx-get="%s?du=2026-06-01&amp;au=2026-06-30&amp;tout=1"' % self.supplier_url()
        )

    def test_the_link_replaces_the_whole_panel_and_not_the_foot_it_sits_in(self):
        """The link is in the foot of the table, so `closest td` is that
        foot's own cell - and the panel would be redrawn inside itself."""
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertContains(panel, 'class="row-panel"')
        self.assertContains(panel, 'hx-target="closest .row-panel" hx-swap="outerHTML"')
        self.assertNotContains(panel, 'hx-target="closest td"')


class ThePanelsAreFetchedWithTheWindowTests(PurchaseWindowTestCase):
    """One string built in the view and appended to the three
    `data-movements-url`: a `{% if %}` fragment per attribute is exactly
    where one of them gets forgotten."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        # Two postes on one document, so the poste rows - the third
        # `data-movements-url` - are drawn at all.
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        self.provisions = make_product(supplier=self.bailleur, raw_name="PROVISIONS", is_expense=True)
        bill = make_invoice(supplier=self.bailleur, invoice_date=date(2026, 2, 5))
        make_invoice_line(invoice=bill, product=self.loyer, total_ht="500", vat_rate=D("0.20"))
        make_invoice_line(invoice=bill, product=self.provisions, total_ht="80", vat_rate=D("0.20"))

    def test_the_three_urls_carry_the_two_dates(self):
        response = self.page(**FEBRUARY)
        self.assertEqual(response.context["panel_window_query"], "?du=2026-02-01&au=2026-02-28")
        for url in (
            reverse("inventory:stock_type_movements", args=[self.vodka.pk]),
            reverse("inventory:charge_supplier_documents", args=[self.bailleur.pk]),
            reverse("inventory:charge_documents", args=[self.loyer.pk]),
        ):
            with self.subTest(url=url):
                self.assertContains(
                    response, 'data-movements-url="%s?du=2026-02-01&amp;au=2026-02-28"' % url
                )

    def test_with_nothing_asked_they_stay_bare(self):
        response = self.page()
        self.assertEqual(response.context["panel_window_query"], "")
        url = reverse("inventory:stock_type_movements", args=[self.vodka.pk])
        self.assertContains(response, 'data-movements-url="%s"' % url)

    def test_the_curves_are_never_windowed(self):
        """A curve IS a history: narrowed to one month it is two points and
        « pas assez d'historique », which is a feature removed rather than a
        window applied."""
        response = self.page(**FEBRUARY)
        for url in (
            reverse("inventory:stock_type_price_history", args=[self.vodka.pk]),
            reverse("inventory:charge_supplier_history", args=[self.bailleur.pk]),
            reverse("inventory:charge_history", args=[self.loyer.pk]),
        ):
            with self.subTest(url=url):
                self.assertContains(response, 'data-price-history-url="%s"' % url)

    def test_a_curve_asked_with_dates_still_draws_them_all(self):
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="60")
        curve = self.client.get(
            reverse("inventory:stock_type_price_history", args=[self.vodka.pk]), FEBRUARY
        )
        self.assertTrue(curve.context["has_enough_data"])


class AnInventaireKeepsItsAllHistoryPanelsTests(PurchaseWindowTestCase):
    """A chosen inventaire is two physical counts with its own arithmetic,
    and CLAUDE.md documents what its rows open as the whole history. The
    dates are disabled under it, so they must not reach the panels either -
    which falls out of building the query from the window APPLIED rather
    than from the one the URL asked for."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 1, 10), quantity="12", total_ht="240")
        self.take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))

    def test_the_panel_urls_carry_nothing(self):
        response = self.page(inventaire=self.take.pk, **FEBRUARY)
        self.assertEqual(response.context["panel_window_query"], "")
        url = reverse("inventory:stock_type_movements", args=[self.vodka.pk])
        self.assertContains(response, 'data-movements-url="%s"' % url)

    def test_and_the_page_does_not_claim_the_panels_are_windowed(self):
        response = self.page(inventaire=self.take.pk, **FEBRUARY)
        self.assertNotContains(response, "ses achats sont ceux de ces dates")
