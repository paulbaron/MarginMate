"""« Achats », onglet Documents : la période « du … au … ».

The owner asked to see the purchases between two dates. The window itself is
`common.date_range` and is tested there; what is tested here is everything
this page can silently get wrong with one:

* **both ends are included** - a document dated the 28th is in « au 28 »;
* **the chip counts follow the window**, or « Tickets 412 » sits above a
  page of nine;
* **every chip and every link carries the window**, or it vanishes on the
  next click and the reader is looking at three years of documents believing
  they are looking at February;
* **« Sans date » drops it**: a document with no date is in no window, so
  that chip under one would open an empty page for ever;
* **an import's own list ignores it**, exactly as it already ignores the
  chips: it is the list of what that import brought in, not a search;
* **a search, a supplier and a window compose** - each of the two « Effacer »
  clears its own and leaves the other alone;
* **an unreadable date is no window**, never a 500.

Data invented.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices.models import Invoice, ReceiptBatch, Supplier
from tests.factories import make_invoice

#: A ticket is a document that carries checks (workspace.IS_TICKET); an
#: invoice is one that carries none.
CHECKS = [{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}]

FEBRUARY = "du=2026-02-01&au=2026-02-28"


class DocumentWindowTests(TestCase):
    def setUp(self):
        self.metro = Supplier.objects.get(code="METRO")
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.url = reverse("invoices:invoice_list")
        # One before, one inside, one after - plus the two boundary days,
        # which are the ones an off-by-one silently loses.
        self.before = make_invoice(supplier=self.metro, invoice_date=date(2026, 1, 31))
        self.opening = make_invoice(supplier=self.metro, invoice_date=date(2026, 2, 1))
        self.inside = make_invoice(supplier=self.metro, invoice_date=date(2026, 2, 14))
        self.closing = make_invoice(supplier=self.metro, invoice_date=date(2026, 2, 28))
        self.after = make_invoice(supplier=self.metro, invoice_date=date(2026, 3, 1))

    def get(self, query: str = ""):
        return self.client.get(f"{self.url}?{query}" if query else self.url)

    def rows(self, response):
        return [invoice.pk for invoice in response.context["invoices"]]

    def chip(self, response, key: str) -> dict:
        return next(chip for chip in response.context["chips"] if chip["key"] == key)

    def undated(self):
        invoice = make_invoice(supplier=self.metro, invoice_number="SANS-DATE")
        Invoice.objects.filter(pk=invoice.pk).update(invoice_date=None)
        return invoice

    def test_only_the_documents_between_the_two_dates(self):
        response = self.get("du=2026-02-02&au=2026-02-20")
        self.assertEqual(self.rows(response), [self.inside.pk])

    def test_both_ends_are_included(self):
        """« au 28 » means the 28th: half-open, this quietly drops a day -
        and the stock pages' own helpers ARE half-open."""
        response = self.get(FEBRUARY)
        self.assertEqual(
            sorted(self.rows(response)), sorted([self.opening.pk, self.inside.pk, self.closing.pk])
        )
        self.assertNotIn(self.before.pk, self.rows(response))
        self.assertNotIn(self.after.pk, self.rows(response))

    def test_one_end_alone_is_a_window(self):
        since = self.rows(self.get("du=2026-02-28"))
        self.assertEqual(sorted(since), sorted([self.closing.pk, self.after.pk]))
        until = self.rows(self.get("au=2026-02-01"))
        self.assertEqual(sorted(until), sorted([self.before.pk, self.opening.pk]))

    def test_no_window_is_every_document(self):
        self.assertEqual(len(self.rows(self.get())), 5)
        self.assertFalse(self.get().context["date_window"])

    def test_an_unreadable_date_is_the_whole_list_not_a_500(self):
        """This arrives from a query string: a stale bookmark, a hand-typed
        URL, a browser with no date input where someone wrote « hier »."""
        for query in ("du=n-importe-quoi", "du=2026-02-30&au=2026-13-01", "du=&au="):
            with self.subTest(query=query):
                response = self.get(query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.rows(response)), 5)
                self.assertFalse(response.context["date_window"])

    def test_backwards_dates_are_swapped_rather_than_answered_with_nothing(self):
        self.assertEqual(sorted(self.rows(self.get("du=2026-02-28&au=2026-02-01"))), sorted(self.rows(self.get(FEBRUARY))))

    # ------------------------------------------------------------------ chips

    def test_the_chip_counts_follow_the_window(self):
        """Counted over everything, « Tickets 412 » sat above a page of 9."""
        ticket = make_invoice(supplier=self.sabbh, invoice_date=date(2026, 2, 10), parse_checks=CHECKS)
        make_invoice(supplier=self.sabbh, invoice_date=date(2026, 7, 10), parse_checks=CHECKS)
        response = self.get(FEBRUARY)
        self.assertEqual(self.chip(response, "")["count"], 4)
        self.assertEqual(self.chip(response, "factures")["count"], 3)
        self.assertEqual(self.chip(response, "tickets")["count"], 1)
        # And the count is the list: the chip is a promise about the page it opens.
        self.assertEqual(self.chip(response, "")["count"], len(self.rows(response)))
        self.assertEqual(
            self.chip(response, "tickets")["count"],
            len(self.rows(self.get(f"{FEBRUARY}&filtre=tickets"))),
        )
        self.assertIn(ticket.pk, self.rows(self.get(f"{FEBRUARY}&filtre=tickets")))

    def test_the_chip_counts_include_both_boundary_days(self):
        """The rows are known to include the 1st and the 28th; the counts
        beside them are a second, separate query, and a chip counted over a
        window one day short says 2 above a page of 4 - the same lie the
        other way round. Both boundary days are tickets here, so the count
        that would lose one is the one being read."""
        make_invoice(supplier=self.sabbh, invoice_date=date(2026, 2, 1), parse_checks=CHECKS)
        make_invoice(supplier=self.sabbh, invoice_date=date(2026, 2, 28), parse_checks=CHECKS)
        make_invoice(supplier=self.sabbh, invoice_date=date(2026, 1, 31), parse_checks=CHECKS)
        make_invoice(supplier=self.sabbh, invoice_date=date(2026, 3, 1), parse_checks=CHECKS)
        response = self.get(FEBRUARY)
        self.assertEqual(self.chip(response, "tickets")["count"], 2)
        self.assertEqual(self.chip(response, "")["count"], 5)
        # And the promise the chip makes is kept by the page it opens.
        self.assertEqual(
            self.chip(response, "tickets")["count"],
            len(self.rows(self.get(f"{FEBRUARY}&filtre=tickets"))),
        )

    def test_what_the_page_says_it_holds_is_counted_over_the_window(self):
        """« Les 250 plus récents. N de plus dans cette liste » and the note
        in words are both `listed_count`. Counted over every document, they
        would offer the three years outside February as « de plus dans cette
        liste » - and the note would say 880 over a page of 30."""
        for day in range(1, 29):
            make_invoice(supplier=self.metro, invoice_date=date(2026, 2, day))
        outside = [make_invoice(supplier=self.metro, invoice_date=date(2026, 6, 1)) for _ in range(300)]
        response = self.get(FEBRUARY)
        # 28 of February plus the three of setUp that fall in it.
        self.assertEqual(response.context["listed_count"], 31)
        self.assertEqual(len(self.rows(response)), 31)
        self.assertEqual(response.context["hidden_count"], 0)
        self.assertContains(response, "Du 01/02/2026 au 28/02/2026")
        self.assertContains(response, "31 documents")
        self.assertNotContains(response, "de plus dans cette liste")
        # Without the window the same page pages, which is what makes the
        # figures above the window's own rather than a fixed 0.
        whole = self.get()
        self.assertEqual(whole.context["listed_count"], len(outside) + 33)
        self.assertEqual(whole.context["hidden_count"], len(outside) + 33 - 250)

    def test_every_chip_carries_the_window_except_sans_date(self):
        """A chip that drops it means the window silently vanishes on the
        next click - the most likely bug on this page."""
        self.undated()
        response = self.get(FEBRUARY)
        for chip in response.context["chips"]:
            with self.subTest(chip=chip["key"]):
                if chip["key"] == "sans-date":
                    self.assertNotIn("du=", chip["url"])
                    self.assertNotIn("au=", chip["url"])
                else:
                    self.assertIn("du=2026-02-01", chip["url"])
                    self.assertIn("au=2026-02-28", chip["url"])

    def test_an_undated_document_is_in_no_window_and_waits_under_sans_date(self):
        nowhere = self.undated()
        self.assertNotIn(nowhere.pk, self.rows(self.get(FEBRUARY)))
        # Its chip counts every undated document, window or not: narrowed by
        # one it would always say 0, and disappear.
        self.assertEqual(self.chip(self.get(FEBRUARY), "sans-date")["count"], 1)
        # And selecting it drops the window rather than opening an empty
        # page - the form is not drawn either, since it would be a control
        # this list cannot honour.
        response = self.get(f"{FEBRUARY}&filtre=sans-date")
        self.assertEqual(self.rows(response), [nowhere.pk])
        self.assertFalse(response.context["date_window"])
        self.assertNotContains(response, 'name="du"')

    def test_the_undated_warning_survives_a_window(self):
        self.undated()
        response = self.get(FEBRUARY)
        self.assertEqual(response.context["undated_count"], 1)
        self.assertContains(response, "sans date")

    # ------------------------------------------------- what composes with it

    def test_a_window_and_a_search_are_both(self):
        eau = Supplier.objects.create(code="EAU", name="Eau De Paris")
        wanted = make_invoice(supplier=eau, invoice_date=date(2026, 2, 12))
        make_invoice(supplier=eau, invoice_date=date(2026, 6, 12))
        response = self.get(f"{FEBRUARY}&q=eau+de+paris")
        self.assertEqual(self.rows(response), [wanted.pk])
        self.assertEqual(response.context["found_count"], 1)
        # The search keeps the window it was typed under, and the other way round.
        self.assertContains(response, 'name="du" value="2026-02-01"', count=2)
        self.assertContains(response, 'name="q" value="eau de paris"', count=2)

    def test_a_window_and_one_supplier_are_both(self):
        eau = Supplier.objects.create(code="EAU", name="Eau De Paris")
        wanted = make_invoice(supplier=eau, invoice_date=date(2026, 2, 12))
        make_invoice(supplier=eau, invoice_date=date(2026, 6, 12))
        response = self.get(f"{FEBRUARY}&fournisseur={eau.pk}")
        self.assertEqual(self.rows(response), [wanted.pk])
        # « 875 de plus dans cette liste » over one document is the same lie
        # as a chip over an empty page.
        self.assertEqual(response.context["listed_count"], 1)
        self.assertEqual(response.context["hidden_count"], 0)
        # The window keeps the supplier it was typed under, both forms.
        self.assertContains(response, f'name="fournisseur" value="{eau.pk}"', count=2)
        # Its ✕ drops the supplier and keeps the dates.
        self.assertIn("du=2026-02-01", response.context["clear_supplier_url"])
        self.assertNotIn("fournisseur", response.context["clear_supplier_url"])

    def test_each_effacer_clears_its_own(self):
        """One that cleared the lot silently undid the other: the dates went
        with the search, and the search with the dates."""
        response = self.get(f"{FEBRUARY}&q=metro&filtre=factures")
        search = response.context["clear_search_url"]
        self.assertNotIn("q=", search)
        self.assertIn("du=2026-02-01", search)
        self.assertIn("filtre=factures", search)
        window = response.context["clear_window_url"]
        self.assertNotIn("du=", window)
        self.assertNotIn("au=", window)
        self.assertIn("q=metro", window)
        self.assertIn("filtre=factures", window)
        self.assertContains(response, "Effacer les dates")
        self.assertContains(response, "Effacer la recherche")

    def test_clearing_the_dates_keeps_the_supplier_it_was_typed_under(self):
        """The other way round is tested above (the ✕ keeps the dates).
        « Effacer les dates » on a supplier's documents must leave the reader
        on that supplier, or the page silently widens to every supplier."""
        eau = Supplier.objects.create(code="EAU", name="Eau De Paris")
        make_invoice(supplier=eau, invoice_date=date(2026, 2, 12))
        window = self.get(f"{FEBRUARY}&fournisseur={eau.pk}").context["clear_window_url"]
        self.assertNotIn("du=", window)
        self.assertNotIn("au=", window)
        self.assertIn(f"fournisseur={eau.pk}", window)

    def test_deleting_from_the_list_comes_back_to_the_window(self):
        """The bulk bar's `next` is where the page returns after a deletion.
        Without the window the reader lands on three years of documents and
        goes on ticking boxes there."""
        response = self.get(FEBRUARY)
        self.assertContains(
            response, 'name="next" value="/invoices/?du=2026-02-01&amp;au=2026-02-28"'
        )

    def test_the_form_keeps_the_filter_the_search_and_the_supplier(self):
        """A window that threw away the tab the reader was on would be typed
        again on every click."""
        response = self.get(f"{FEBRUARY}&filtre=factures&q=metro")
        self.assertContains(response, 'name="filtre" value="factures"', count=2)
        self.assertContains(response, 'name="du" value="2026-02-01"', count=2)
        self.assertContains(response, 'name="au" value="2026-02-28"', count=2)
        # Neither end is required: « depuis le 1er février » is a window.
        self.assertNotContains(response, 'type="date" name="du" required')

    def test_tout_afficher_and_the_reload_keep_the_window(self):
        """« tout afficher » is built from request.get_full_path, and the
        list re-fetches itself with it when an import ends."""
        response = self.get(FEBRUARY)
        self.assertIn("du=2026-02-01", response.context["show_all_url"])
        self.assertIn("au=2026-02-28", response.context["show_all_url"])
        self.assertContains(response, f'hx-get="{self.url}?du=2026-02-01&amp;au=2026-02-28"')

    def test_a_document_outside_the_window_is_not_slipped_into_the_answer(self):
        """The document just imported is shown whatever its date - but not
        under two dates it falls outside of."""
        response = self.get(f"{FEBRUARY}&surligner={self.after.pk}")
        self.assertNotIn(self.after.pk, self.rows(response))
        self.assertIn(self.after.pk, self.rows(self.get(f"surligner={self.after.pk}")))

    # ----------------------------------------------------------- what it says

    def test_the_page_says_the_window_in_words(self):
        self.assertContains(self.get(FEBRUARY), "Du 01/02/2026 au 28/02/2026")
        self.assertContains(self.get("du=2026-02-01"), "Depuis le 01/02/2026")
        self.assertContains(self.get("au=2026-02-28"), "Jusqu'au 28/02/2026")

    def test_an_empty_window_reads_as_an_empty_period(self):
        """Not as « aucun achat pour le moment : ajoutez des tickets »,
        which is what an empty page said under two dates."""
        response = self.get("du=2030-01-01&au=2030-01-31")
        self.assertEqual(self.rows(response), [])
        self.assertContains(response, "Aucun document sur cette période")
        self.assertContains(response, "Du 01/01/2030 au 31/01/2030")
        self.assertNotContains(response, "Aucun achat pour le moment")
        # The way back out of an empty period is the one link that must NOT
        # carry it. Asserted the other way round ("du=" is in that url plus
        # "du="), this said nothing at all and could never fail.
        self.assertNotIn("du=", response.context["clear_window_url"])
        self.assertNotIn("au=", response.context["clear_window_url"])
        self.assertContains(response, "Voir toutes les dates")


class ImportListIgnoresTheWindowTests(TestCase):
    """An import's list is the documents that import brought in - an explicit
    list, not a search, which is why it already ignores the filter chips."""

    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.old = make_invoice(supplier=self.sabbh, invoice_date=date(2025, 4, 4), parse_checks=CHECKS)
        self.recent = make_invoice(supplier=self.sabbh, invoice_date=date(2026, 2, 14), parse_checks=CHECKS)
        self.batch = ReceiptBatch.objects.create(
            status=ReceiptBatch.Status.SUCCESS,
            results=[
                {"name": "a.jpg", "status": "ok", "invoice_id": self.old.pk, "shop": "Sabbh", "total": "1.00"},
                {"name": "b.jpg", "status": "ok", "invoice_id": self.recent.pk, "shop": "Sabbh", "total": "2.00"},
            ],
        )

    def test_a_batch_list_is_untouched_by_a_window(self):
        url = reverse("invoices:receipt_batch", args=[self.batch.pk])
        response = self.client.get(f"{url}?{FEBRUARY}")
        self.assertEqual(
            sorted(invoice.pk for invoice in response.context["invoices"]),
            sorted([self.old.pk, self.recent.pk]),
        )
        # And the page does not offer a window it would not honour.
        self.assertFalse(response.context["date_window"])
        self.assertNotContains(response, 'name="du"')


class WindowedReviewDatesTests(TestCase):
    """« Vérifiés récemment » is about `reviewed_at`; the window is about the
    document's own date. Both at once must not throw one away."""

    def setUp(self):
        self.sabbh = Supplier.objects.get(code="SABBH")
        self.url = reverse("invoices:invoice_list")
        self.checked = make_invoice(
            supplier=self.sabbh, invoice_date=date(2026, 2, 14), parse_checks=CHECKS, reviewed_at=timezone.now()
        )
        self.checked_elsewhere = make_invoice(
            supplier=self.sabbh, invoice_date=date(2026, 8, 14), parse_checks=CHECKS, reviewed_at=timezone.now()
        )

    def test_a_filter_on_another_date_still_obeys_the_window(self):
        response = self.client.get(f"{self.url}?{FEBRUARY}&filtre=verifies")
        self.assertEqual([invoice.pk for invoice in response.context["invoices"]], [self.checked.pk])
        self.assertEqual(
            next(chip for chip in response.context["chips"] if chip["key"] == "verifies")["count"], 1
        )
