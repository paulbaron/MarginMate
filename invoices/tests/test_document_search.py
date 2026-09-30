"""What a typed search finds: « fournisseur 2025 », « fournisseur 09/2025 ».

`invoices.workspace.documents_matching` answers two pages - the « Achats »
list of documents and the box on each « Banque » row that attaches a document
to a payment - and it promises this:

* **the words of a query narrow each other.** A token that is exactly a date
  (a year, a month and a year, a full day) narrows the date; the others match
  the supplier's name or the document's number; and they AND together, so
  « fournisseur 2025 » is that supplier's documents of 2025 and not every
  document of 2025 plus every document of that supplier;
* **everything that works alone goes on working**: a bare supplier, a bare
  number, a bare amount, a bare date written 12/07/2026, 07/2026 or 2026;
* **a number is not a date.** A document number carries digits, dashes and
  slashes of its own: a token is read as a date only when it is exactly a
  date, and a token is matched against the number whether or not it is also
  a date, so a document numbered after a year is still found by that year;
* **it is a query string, so nothing it can hold raises.** A day that is no
  day, a month that is no month, two dates that contradict each other, a
  paragraph pasted in: an empty answer or the whole list, never an error.

The amount stays as weak as it was (`Invoice.printed_total_ttc` is only set
where a document printed its own total), and combining it with a word cannot
make it weaker: a token is matched against the amount as well as the name and
the number, never instead of them.

Every supplier, number and amount below is invented. This repository is
public.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from invoices.models import Invoice
from invoices.workspace import documents_matching
from tests.factories import make_invoice, make_supplier

#: Invented, and deliberately not one of the seeded suppliers: a supplier the
#: owner really buys from has no business in a test in a public repository.
WHOLESALER = "Grossiste Invente"
SPRING = "Sources Du Causse"  # a supplier whose name is several words
GROCER = "Épicerie Du Coin"  # accents and case


class DocumentSearchTests(TestCase):
    def setUp(self):
        self.wholesaler = make_supplier(name=WHOLESALER)
        self.spring = make_supplier(name=SPRING)
        self.grocer = make_supplier(name=GROCER)
        # One wholesaler, four dates: this is what « fournisseur 2025 » and
        # « fournisseur 09/2025 » have to tell apart.
        self.september = make_invoice(supplier=self.wholesaler, invoice_number="A-1001", invoice_date=date(2025, 9, 14))
        self.march = make_invoice(supplier=self.wholesaler, invoice_number="A-1002", invoice_date=date(2025, 3, 2))
        self.old = make_invoice(supplier=self.wholesaler, invoice_number="A-0900", invoice_date=date(2024, 9, 14))
        self.july = make_invoice(supplier=self.wholesaler, invoice_number="A-1177", invoice_date=date(2026, 7, 12))
        # Another supplier, same month: the year alone must not be enough.
        self.other = make_invoice(supplier=self.spring, invoice_number="B-4321", invoice_date=date(2025, 9, 20))

    def found(self, query):
        return set(documents_matching(Invoice.objects.all(), query).values_list("pk", flat=True))

    # ------------------------------------------------ the words narrow each other

    def test_a_supplier_and_a_year(self):
        """« Grossiste 2025 »: no supplier is called that, no number holds it
        and the whole string is no date - so the query used to find nothing
        at all."""
        self.assertEqual(self.found("Grossiste 2025"), {self.september.pk, self.march.pk})

    def test_a_supplier_and_a_month(self):
        self.assertEqual(self.found("Grossiste 09/2025"), {self.september.pk})

    def test_a_supplier_and_a_full_day(self):
        self.assertEqual(self.found("Grossiste 12/07/2026"), {self.july.pk})

    def test_a_supplier_of_several_words_with_a_year(self):
        """Each word narrows: « Sources Du Causse 2026 » is three words and a
        date, and the three words are one name."""
        self.assertEqual(self.found("Sources Du Causse 2025"), {self.other.pk})
        self.assertEqual(self.found("Sources Du Causse 2026"), set())

    def test_a_number_and_a_word(self):
        self.assertEqual(self.found("Grossiste A-1001"), {self.september.pk})
        # The number is that document's; the word is another supplier's.
        self.assertEqual(self.found("Sources A-1001"), set())

    def test_case_accents_and_extra_spaces(self):
        """Typed in a hurry: doubled spaces, no capitals. The accents are the
        name's own - the database compares them as they are written."""
        grocery = make_invoice(supplier=self.grocer, invoice_number="C-3", invoice_date=date(2026, 2, 4))
        self.assertEqual(self.found("  épicerie   DU coin   2026 "), {grocery.pk})

    # ------------------------------------------------------ what worked alone

    def test_a_bare_supplier_a_bare_number_and_a_bare_amount(self):
        priced = make_invoice(
            supplier=self.spring,
            invoice_number="B-5000",
            invoice_date=date(2026, 4, 6),
            printed_total_ttc=Decimal("274.38"),
        )
        self.assertEqual(self.found("grossiste"), {self.september.pk, self.march.pk, self.old.pk, self.july.pk})
        self.assertEqual(self.found("A-1002"), {self.march.pk})
        for written in ("274,38", "274.38"):
            with self.subTest(written=written):
                self.assertEqual(self.found(written), {priced.pk})

    def test_a_bare_date_in_its_three_shapes(self):
        self.assertEqual(self.found("14/09/2025"), {self.september.pk})
        self.assertEqual(self.found("09/2025"), {self.september.pk, self.other.pk})
        self.assertEqual(self.found("2024"), {self.old.pk})

    def test_an_amount_with_a_word_narrows_and_does_not_widen(self):
        """`printed_total_ttc` is set only where a document printed its own
        total, so an amount finds few rows - a word beside it may only take
        rows away, never add the wrong one."""
        priced = make_invoice(
            supplier=self.wholesaler,
            invoice_number="A-2000",
            invoice_date=date(2026, 3, 9),
            printed_total_ttc=Decimal("48.20"),
        )
        self.assertEqual(self.found("grossiste 48,20"), {priced.pk})
        self.assertEqual(self.found("sources 48,20"), set())

    # ------------------------------------------------- a number is not a date

    def test_a_number_of_digits_and_dashes_stays_a_number(self):
        """« 047-031286 » is shaped like a date and is not one: three digits,
        then six. Read as a date it would be a crash or an empty page; it is
        the number of the document the reader is holding."""
        numbered = make_invoice(supplier=self.spring, invoice_number="047-031286", invoice_date=date(2026, 1, 8))
        self.assertEqual(self.found("047-031286"), {numbered.pk})
        self.assertEqual(self.found("Sources 047-031286"), {numbered.pk})

    def test_a_year_inside_a_number_is_still_found(self):
        """A document numbered after a year it is not dated in: the token is
        matched against the number as well as the date, so both answer."""
        numbered = make_invoice(supplier=self.spring, invoice_number="FA-2031-118", invoice_date=date(2026, 5, 5))
        self.assertIn(numbered.pk, self.found("2031"))
        self.assertEqual(self.found("Sources 2031"), {numbered.pk})

    # ------------------------------------------- it is a query string, never a 500

    def test_a_token_shaped_like_a_date_that_is_no_date(self):
        for query in ("13/2025", "32/07/2026", "01/01/0000", "Grossiste 32/07/2026"):
            with self.subTest(query=query):
                self.assertEqual(self.found(query), set())

    def test_two_dates_that_contradict_each_other(self):
        self.assertEqual(self.found("Grossiste 2025 2026"), set())
        self.assertEqual(self.found("09/2025 03/2025"), set())

    def test_an_implausible_year(self):
        self.assertEqual(self.found("Grossiste 1312"), set())
        self.assertEqual(self.found("01/01/1312"), set())

    def test_an_empty_query_is_the_whole_list(self):
        everything = set(Invoice.objects.values_list("pk", flat=True))
        for query in ("", "   ", "\t "):
            with self.subTest(query=query):
                self.assertEqual(self.found(query), everything)

    def test_a_paragraph_pasted_in_answers(self):
        """A query string holds whatever was in the clipboard. It may find
        nothing; it may not fail, and it may not build a query so deep the
        database refuses it."""
        self.assertEqual(self.found(" ".join(f"mot{n}" for n in range(200))), set())


class SearchHelpTests(TestCase):
    """Both pages say what may be typed - a search whose rules nobody states
    is a box people type one word into for ever."""

    def test_the_purchases_list_says_a_supplier_and_a_date_may_be_typed(self):
        page = self.client.get(reverse("invoices:invoice_list")).content.decode()
        self.assertIn("fournisseur 09/2025", page)

    def test_the_purchases_list_says_it_again_when_a_search_finds_nothing(self):
        response = self.client.get(reverse("invoices:invoice_list"), {"q": "introuvable"})
        self.assertEqual(response.context["found_count"], 0)
        self.assertIn("fournisseur 09/2025", response.content.decode())
