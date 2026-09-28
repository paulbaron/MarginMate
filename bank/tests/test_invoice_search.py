"""Finding the invoice a bank line paid, without losing the page.

The pick-list beside each line offers the unpaid invoices AT ITS DATES, and
only the first fifteen of them. Everything else - the invoice of six months
ago, the one whose amount does not match, the one another line already pays
- is reached by typing a search, and that search used to be a GET on the
whole page: it reloaded « Banque », scrolled the reader back to the top and
left them to find their row again.

So the search box asks for its results alone and swaps them into its own
row. The page does not move, and the plain form underneath still works with
no JavaScript at all - which is the only reason the GET on the page is kept.

Every supplier, payee and figure below is invented.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bank.models import BankTransaction, InvoicePayment
from bank.tests.test_reconcile import Fixtures, debit_row
from tests.factories import make_supplier

WHOLESALER = "GROSSISTE"
PAYEE = "GROSSISTE INVENTE"


class SearchPage(Fixtures):
    def setUp(self):
        make_supplier(code=WHOLESALER, name="Grossiste Inventé")
        self.load(debit_row(date(2026, 7, 9), PAYEE, "120,00"))
        self.line = BankTransaction.objects.get(counterparty=PAYEE)
        # Dated far from the debit on purpose: the pick-list is built from
        # the dates around the line, so this one is only ever reachable by
        # searching for it.
        self.far = self.invoice(WHOLESALER, date(2025, 11, 3), "500.00", invoice_number="F-LOINTAINE")
        self.near = self.invoice(WHOLESALER, date(2026, 7, 2), "100.00", invoice_number="F-PROCHE")

    def search(self, query, **extra):
        return self.client.get(
            reverse("bank:invoice_search", args=[self.line.pk]), {"recherche": query, **extra}
        )


class FragmentTests(SearchPage, TestCase):
    def test_it_answers_with_the_results_alone(self):
        """A fragment, not the page: swapped into the row, a whole document
        would put a second navigation inside a table cell."""
        body = self.search("F-LOINTAINE").content.decode()
        self.assertIn("F-LOINTAINE", body)
        self.assertNotIn("<html", body.lower())
        self.assertNotIn("</nav>", body.lower())

    def test_it_finds_an_invoice_the_pick_list_never_offers(self):
        """Eight months before the debit: outside the window the choices are
        drawn from, and the whole reason the box exists."""
        page = self.client.get(reverse("bank:bank_home"))
        self.assertNotContains(page, "F-LOINTAINE")
        self.assertContains(self.search("F-LOINTAINE"), "F-LOINTAINE")

    def test_the_results_carry_the_form_that_links_them(self):
        body = self.search("F-LOINTAINE").content.decode()
        self.assertIn(f'value="{self.far.pk}"', body)
        self.assertIn('name="action" value="link"', body)
        self.assertIn(reverse("bank:bank_line_action", args=[self.line.pk]), body)

    def test_what_it_finds_can_actually_be_linked(self):
        self.client.post(
            reverse("bank:bank_line_action", args=[self.line.pk]),
            {"action": "link", "invoice": [self.far.pk]},
        )
        self.assertEqual(
            set(self.line.payments.values_list("invoice__invoice_number", flat=True)), {"F-LOINTAINE"}
        )

    def test_an_empty_search_finds_nothing_and_says_nothing(self):
        body = self.search("").content.decode()
        self.assertNotIn("F-LOINTAINE", body)
        self.assertNotIn("Aucun document", body)

    def test_a_search_matching_nothing_says_so(self):
        self.assertContains(self.search("ZZZZ-INTROUVABLE"), "Aucun document")

    def test_an_invoice_already_on_this_line_is_marked_rather_than_offered(self):
        InvoicePayment.objects.create(
            transaction=self.line, invoice=self.far, method=InvoicePayment.Method.MANUAL
        )
        self.assertContains(self.search("F-LOINTAINE"), "déjà rattachée à cette opération")

    def test_it_says_which_other_line_already_pays_what_it_found(self):
        other = BankTransaction.objects.create(
            operation_date=date(2026, 3, 2), amount=Decimal("-500.00"),
            label="PRLV AUTRE", fingerprint="fp-autre",
        )
        InvoicePayment.objects.create(
            transaction=other, invoice=self.far, method=InvoicePayment.Method.MANUAL
        )
        self.assertContains(self.search("F-LOINTAINE"), "Déjà réglée par l'opération")

    def test_an_income_line_is_refused(self):
        """No form is drawn on an income row, so a search on one is stale or
        crafted - and an entry of money settles no invoice."""
        income = BankTransaction.objects.create(
            operation_date=date(2026, 7, 9), amount=Decimal("900.00"),
            label="VIR RECETTE", fingerprint="fp-recette",
        )
        answer = self.client.get(
            reverse("bank:invoice_search", args=[income.pk]), {"recherche": "F-LOINTAINE"}
        )
        self.assertNotContains(answer, "F-LOINTAINE")

    def test_an_unknown_line_is_not_found(self):
        self.assertEqual(self.client.get(reverse("bank:invoice_search", args=[999999])).status_code, 404)


class SearchBoxTests(SearchPage, TestCase):
    def test_the_box_asks_the_fragment_for_its_results(self):
        page = self.client.get(reverse("bank:bank_home")).content.decode()
        self.assertIn(reverse("bank:invoice_search", args=[self.line.pk]), page)
        self.assertIn(f'id="resultats-{self.line.pk}"', page)

    def test_it_searches_as_the_reader_types(self):
        """Without this the box needs a click to answer, which is the page
        reload again with extra steps."""
        page = self.client.get(reverse("bank:bank_home")).content.decode()
        self.assertIn("hx-trigger", page)
        self.assertIn("delay:", page)

    def test_the_plain_form_underneath_still_works(self):
        """No JavaScript: the GET on the page is what answers, exactly as
        before. It is kept for that and for nothing else."""
        page = self.client.get(
            reverse("bank:bank_home"), {"ligne": self.line.pk, "recherche": "F-LOINTAINE"}
        )
        self.assertContains(page, "F-LOINTAINE")
