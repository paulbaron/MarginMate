"""Les bords de la fenêtre, une fois la ligne déroulée.

`test_panel_window.py` pins what a row of « Produits & charges » opens under
« Du … au … ». This file is the review of it: the edges this codebase gets
bitten by, and the claims the panels make that were said rather than proved.

What it adds, each one a mutation the shipped tests let through or never
looked at:

* **the charge panel's own edges.** An article's panel is narrowed in Python
  (`DateRange.holds`) and its two edge days are pinned; a charge's is
  narrowed in SQL (`DateRange.limit`), and turning either `__gte`/`__lte`
  into a strict comparison changed nothing anybody was asserting. A rent
  billed on the 1st and one billed on the 28th are in the month, and the
  months either side are out;
* **an empty windowed panel is really empty.** « du 01/06/2026 au
  30/06/2026 » appears in the foot of a full panel too, so naming the window
  proves nothing on its own: what says the filter ran is « Aucun document »
  and no row at all;
* **the way back is a URL that works**, not only a link that is drawn: the
  panel's own « tout l'historique » is fetched here and has to answer with
  the whole history;
* **the panel adds up to its row** as an arithmetic, over several purchases:
  the sum of the lines it lists is the « Total HT » the row prints, and the
  documents it counts are the row's count - the panel is opened to explain
  that figure, and a panel that disagrees with it is this project's oldest
  failure shape;
* and the odds and ends a query string brings: one single day asked twice
  (`?du=&au=` on the same date), two dates the wrong way round, a date that
  is no date at all on the charge panel, a purchase whose invoice carries no
  date, and the list reloaded in place by htmx after a classification -
  which draws the rows, and therefore the URLs the panels are fetched with.

Data invented.
"""

from datetime import date, datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from inventory.models import StockMovement, UnitChoices
from inventory.tests.test_panel_window import ArticlePanelTestCase
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


class ChargePanelEdgesTests(PurchaseWindowTestCase):
    """The rows of the charges fold are narrowed in SQL, so `__gte`/`__lte`
    are the whole of « both ends included » for them - and the bills the
    panels' own tests are written on sit on the 5th of each month, a
    fortnight from either edge, where an off-by-one day is invisible.

    The same rent, month after month: January, February, March.
    """

    def setUp(self):
        super().setUp()
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        for month in (1, 2, 3):
            self.bill(date(2026, month, 5), f"LOYER-2026-{month:02d}", total_ht="500")

    def supplier_url(self):
        return reverse("inventory:charge_supplier_documents", args=[self.bailleur.pk])

    def poste_url(self):
        return reverse("inventory:charge_documents", args=[self.loyer.pk])

    def bill(self, on: date, number: str, total_ht="100"):
        invoice = make_invoice(supplier=self.bailleur, invoice_date=on, invoice_number=number)
        make_invoice_line(invoice=invoice, product=self.loyer, total_ht=total_ht, vat_rate=D("0.20"))
        return invoice

    def test_the_first_and_the_last_day_are_in_the_panel(self):
        self.bill(date(2026, 2, 1), "BORD-PREMIER")
        self.bill(date(2026, 2, 28), "BORD-DERNIER")
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertContains(panel, "BORD-PREMIER")
        self.assertContains(panel, "BORD-DERNIER")

    def test_the_days_just_outside_are_out_of_it(self):
        self.bill(date(2026, 1, 31), "BORD-VEILLE")
        self.bill(date(2026, 3, 1), "BORD-LENDEMAIN")
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertNotContains(panel, "BORD-VEILLE")
        self.assertNotContains(panel, "BORD-LENDEMAIN")

    def test_a_poste_panel_has_the_same_edges(self):
        self.bill(date(2026, 2, 28), "BORD-DERNIER")
        self.bill(date(2026, 3, 1), "BORD-LENDEMAIN")
        panel = self.client.get(self.poste_url(), FEBRUARY)
        self.assertContains(panel, "BORD-DERNIER")
        self.assertNotContains(panel, "BORD-LENDEMAIN")

    def test_one_day_asked_twice_is_that_day(self):
        """« du 5 au 5 » is a day, not an empty page nor everything."""
        panel = self.client.get(self.supplier_url(), {"du": "2026-02-05", "au": "2026-02-05"})
        self.assertContains(panel, "LOYER-2026-02")
        self.assertNotContains(panel, "LOYER-2026-01")

    def test_a_date_that_is_no_date_is_the_whole_history(self):
        """This URL is a query string like the list's: a stale bookmark or a
        browser with no date input lands here, and neither is a 500."""
        panel = self.client.get(self.supplier_url(), {"du": "hier", "au": "2026-02-30"})
        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, "LOYER-2026-01")
        self.assertContains(panel, "LOYER-2026-03")

    def test_an_empty_window_really_holds_nothing(self):
        """Naming the window proves nothing by itself - the foot of a FULL
        panel names it too. What says the filter ran is that there is no row
        and the sentence is « Aucun document »."""
        panel = self.client.get(self.supplier_url(), {"du": "2026-06-01", "au": "2026-06-30"})
        self.assertEqual(panel.context["rows"], [])
        self.assertEqual(panel.context["total_ttc"], D("0"))
        self.assertContains(panel, "Aucun document pour « Bailleur Exemple »")
        self.assertNotContains(panel, "LOYER-2026-01")
        self.assertNotContains(panel, "LOYER-2026-02")

    def test_the_way_back_really_gives_the_whole_history(self):
        """The link is not the feature: the URL behind it is. Fetched, it
        has to answer with the three bills."""
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        whole = self.client.get(panel.context["all_url"])
        for number in ("LOYER-2026-01", "LOYER-2026-02", "LOYER-2026-03"):
            self.assertContains(whole, number)

    def test_the_panel_counts_what_the_row_counts_document_for_document(self):
        """A document filed with nothing read counts on the row, so it
        counts in the panel - and the two figures are compared here as
        figures, not as a rendered string."""
        make_invoice(
            supplier=self.bailleur, invoice_date=date(2026, 2, 20), invoice_number="AVIS-SANS-LIGNE"
        )
        self.bill(date(2026, 2, 28), "BORD-DERNIER")
        (row,) = self.page(**FEBRUARY).context["charge_suppliers"]
        panel = self.client.get(self.supplier_url(), FEBRUARY)
        self.assertEqual(len(panel.context["rows"]), row["documents"])
        self.assertEqual(panel.context["total_ttc"], row["total_ttc"])


class AnArticlePanelAddsUpToItsRowTests(ArticlePanelTestCase):
    """« Acheté 12 » over forty purchases was the bug. The cure is not that
    the panel is shorter - it is that its lines are the row's own figure,
    added up."""

    def test_the_lines_of_the_window_make_the_rows_total_ht(self):
        self.buy(self.vodka, date(2026, 2, 20), quantity="3", total_ht="60")
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        entries = self.panel(**FEBRUARY).context["movements"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(entry["line"].total_ht for entry in entries), row["value_ht"])

    def test_and_the_quantities_make_its_acheté(self):
        self.buy(self.vodka, date(2026, 2, 20), quantity="3", total_ht="60")
        row = self.rows(self.page(**FEBRUARY))["Vodka"]
        entries = self.panel(**FEBRUARY).context["movements"]
        self.assertEqual(sum(entry["movement"].quantity for entry in entries), row["quantity"])

    def test_one_day_asked_twice_is_that_day(self):
        panel = self.panel(du="2026-02-10", au="2026-02-10")
        self.assertContains(panel, "10/02/2026")
        self.assertNotContains(panel, "10/01/2026")

    def test_two_dates_the_wrong_way_round_are_swapped(self):
        """A typo, and what the person means by it is not in doubt - the
        list swaps them, and a panel answering an empty February to the same
        pair would contradict the row it hangs under."""
        panel = self.panel(du="2026-02-28", au="2026-02-01")
        self.assertContains(panel, "10/02/2026")
        self.assertNotContains(panel, "10/01/2026")

    def test_the_way_back_really_gives_the_whole_history(self):
        panel = self.panel(**FEBRUARY)
        whole = self.client.get(panel.context["all_url"])
        self.assertContains(whole, "10/02/2026")
        self.assertContains(whole, "10/01/2026")

    def test_the_way_back_out_of_an_empty_window_works_too(self):
        panel = self.panel(du="2026-06-01", au="2026-06-30")
        self.assertEqual(panel.context["movements"], [])
        whole = self.client.get(panel.context["all_url"])
        self.assertContains(whole, "10/01/2026")


class TheDateColumnIsTheEffectiveDateWindowOrNotTests(ArticlePanelTestCase):
    """The column was changed for every reader, not only for the windowed
    one: a manual correction used to print « — » whatever was asked, and a
    row with no date at all reads as a row nobody dated."""

    def test_a_correction_prints_its_own_date_with_no_window_asked(self):
        make_movement(
            stock_type=self.vodka, quantity="-2", unit_cost_ht="20",
            occurred_on=date(2026, 2, 14), note="Casse",
        )
        panel = self.panel()
        self.assertContains(panel, "14/02/2026")
        self.assertEqual(
            [entry["date"] for entry in panel.context["movements"] if entry["line"] is None],
            [date(2026, 2, 14)],
        )

    def test_a_purchase_whose_invoice_has_no_date_is_dated_by_its_creation(self):
        """An undated document is in no window of the charges fold, but a
        movement always has a date: `created_at` is the last fallback, and
        it is the one the row was summed by."""
        invoice = make_invoice(supplier=self.supplier)
        Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
        line = make_invoice_line(
            invoice=invoice, product=self.product_for(self.vodka), quantity=2, total_ht="40"
        )
        movement = make_movement(
            stock_type=self.vodka, quantity="2", unit_cost_ht="20", invoice_line=line
        )
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime(2026, 2, 18, 12, 0))
        )
        self.assertEqual(self.rows(self.page(**FEBRUARY))["Vodka"]["quantity"], D("14"))
        panel = self.panel(**FEBRUARY)
        self.assertEqual(len(panel.context["movements"]), 2)
        self.assertContains(panel, "18/02/2026")


class WhatTheListPrintsIsWhatThePanelsAnswerTests(PurchaseWindowTestCase):
    """The URL a row carries is fetched by htmx as it stands, so the list
    and the panel are one promise in two places: the reloaded list has to
    carry the window too, an inventaire's rows really do open on everything,
    and the sentence saying which is printed once."""

    def setUp(self):
        super().setUp()
        self.vodka = make_stock_type(name="Vodka", unit=UnitChoices.LITRE, category="Spiritueux")
        self.buy(self.vodka, date(2026, 2, 10), quantity="12", total_ht="240")
        self.buy(self.vodka, date(2026, 1, 10), quantity="6", total_ht="120")
        self.bailleur = make_supplier(
            code="BAILLEUR_X", name="Bailleur Exemple", parser_key="", expenses_only=True
        )
        self.loyer = make_product(supplier=self.bailleur, raw_name="LOYER", is_expense=True)
        bill = make_invoice(supplier=self.bailleur, invoice_date=date(2026, 2, 5))
        make_invoice_line(invoice=bill, product=self.loyer, total_ht="500", vat_rate=D("0.20"))

    def movements_url(self):
        return reverse("inventory:stock_type_movements", args=[self.vodka.pk])

    def test_the_list_reloaded_in_place_still_carries_the_window(self):
        """`stock_catalogue` is what answers the htmx reload after a product
        is classified: a window lost there is a window lost on every row's
        panel for the rest of the visit, without a word."""
        reloaded = self.client.get(reverse("inventory:stock_catalogue"), FEBRUARY)
        self.assertContains(
            reloaded, 'data-movements-url="%s?du=2026-02-01&amp;au=2026-02-28"' % self.movements_url()
        )
        self.assertContains(
            reloaded,
            'data-movements-url="%s?du=2026-02-01&amp;au=2026-02-28"'
            % reverse("inventory:charge_supplier_documents", args=[self.bailleur.pk]),
        )

    def test_the_sentence_about_what_a_row_opens_is_printed_once(self):
        """Twice on one screen is the noise the wording was written to
        avoid: the list says it, the page around it does not."""
        self.assertContains(self.page(**FEBRUARY), "ses achats sont ceux de ces dates", count=1)

    def test_the_charges_fold_no_longer_promises_an_all_history_panel(self):
        """« Ce qu'une ligne ouvre couvre tout l'historique » was true until
        the panels followed the dates, and a house note left standing after
        the behaviour moved is worse than no note at all."""
        windowed = self.page(**FEBRUARY)
        self.assertContains(windowed, "ses documents sont ceux de ces dates")
        self.assertNotContains(windowed, "Ce qu'une ligne ouvre couvre tout l'historique")

    def test_and_says_it_again_when_no_window_is_asked_for(self):
        """Unasked, the panels ARE the whole history - the sentence is true
        again and has to come back, or the page says nothing at all about
        the one thing the row and the panel do not share."""
        self.assertContains(self.page(), "Ce qu'une ligne ouvre couvre tout l'historique")

    def test_the_documents_column_names_the_window_it_counts(self):
        """« Documents (12 mois) » over counts made of two chosen dates is
        the chip reading « Tickets 412 » above a page of nine."""
        windowed = self.page(**FEBRUARY)
        # « période » is the page's own word for two physical counts, so it
        # is not the word for two dates someone typed either.
        self.assertContains(windowed, "Documents (ces dates)")
        self.assertNotContains(windowed, "Documents (12 mois)")
        self.assertNotContains(windowed, "Documents (période)")
        self.assertContains(windowed, "la ligne s'ouvre sur les mêmes")
        self.assertContains(self.page(), "Documents (12 mois)")

    def test_under_an_inventaire_the_url_the_page_prints_opens_everything(self):
        """Asserted through the URL the page actually draws rather than
        through the context alone: an inventaire is two physical counts with
        its own arithmetic, and CLAUDE.md promises its rows the whole
        history."""
        take = make_stock_take(taken_at=timezone.make_aware(datetime(2026, 3, 31, 12, 0)))
        response = self.page(inventaire=take.pk, **FEBRUARY)
        panel = self.client.get(self.movements_url() + response.context["panel_window_query"])
        self.assertContains(panel, "10/01/2026")
        self.assertContains(panel, "10/02/2026")
        self.assertNotContains(panel, "tout l'historique")
