"""« Dépenses »: everything that LEFT the account over a period, by category.

Deliberately a different question from « Marges », which counts what was
INVOICED. Here the base is the statement: every debit is in, invoice or no
invoice, and the categories have to add back up to what the bank took.

What the page promises, and what each test below holds it to:

* a debit an invoice explains takes the categories of what that invoice
  bought - the articles' categories for goods, the supplier for a charge -
  read through `margins.computation.where_it_went` and never worked out
  again here;
* a debit its invoices do not add up to still adds up on this page: the
  invoices are scaled down when they cost MORE than the debit (a document
  settled in two goes must not be counted twice), and the difference is
  money with nothing to explain it when they cost less;
* a debit no invoice explains takes the category a person typed, or the one
  an ignore rule carries - and the page says which of the two it was, since
  a rule changing later must not read as somebody's decision;
* a debit nobody has categorised is « sans catégorie »: counted, listed
  first, and never folded into « Autres »;
* the pie's slices add up to what the pie says it draws, and the categories
  add up to what left the account, to the cent.

Every supplier, article, payee and amount below is invented.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bank import spending
from bank.models import BankTransaction, IgnoreRule, InvoicePayment
from common import PIE_COLORS, DateRange
from inventory.models import UnitChoices
from margins import computation
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_product,
    make_stock_type,
    make_supplier,
)
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

TWENTY = Decimal("0.20")
WINDOW = DateRange(date(2026, 6, 1), date(2026, 6, 30))


def euros(value) -> Decimal:
    return Decimal(value)


class SpendingFixtures:
    """A wholesaler of goods with two article categories, and a landlord
    whose documents are charges."""

    def setUp(self):
        super().setUp()
        self.url = reverse("bank:spending_home")
        self.goods = make_supplier(code="GROS", name="Grossiste Exemple")
        self.landlord = make_supplier(code="BAILLEUR", name="Bailleur Exemple", expenses_only=True)
        self.beer = make_stock_type(name="Blonde Exemple", unit=UnitChoices.LITRE, category="Bières")
        self.wine = make_stock_type(name="Rouge Exemple", unit=UnitChoices.LITRE, category="Vins")
        self.counter = 0

    def invoice(self, supplier, day, *lines, **kwargs):
        """One invoice, `lines` being (article, total_ht) pairs at 20 %."""
        document = make_invoice(supplier=supplier, invoice_date=day, **kwargs)
        for article, total_ht in lines:
            make_invoice_line(
                invoice=document,
                product=make_product(supplier=supplier, stock_type=article),
                total_ht=total_ht,
                vat_rate=TWENTY,
            )
        return document

    def debit(self, day, payee, amount, **kwargs):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"PRLV SEPA {payee} REF/{self.counter:04d}",
            counterparty=payee,
            amount=-euros(amount),
            kind=BankTransaction.Kind.DEBIT,
            fingerprint=f"debit-{self.counter}",
            **kwargs,
        )

    def income(self, day, payee, amount):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=f"VIREMENT {payee}",
            counterparty=payee,
            amount=euros(amount),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint=f"income-{self.counter}",
        )

    def pay(self, line, *invoices):
        for document in invoices:
            InvoicePayment.objects.create(transaction=line, invoice=document, method=InvoicePayment.Method.MANUAL)
        line.settled_by_hand = True
        line.save(update_fields=["settled_by_hand"])
        return line

    def amounts(self, report) -> dict:
        return {category.name: category.amount for category in report.categories}


class WhereADebitWentTests(SpendingFixtures, TestCase):
    def test_a_debit_with_one_invoice_takes_its_articles_categories(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Bières": euros("120.00")})
        self.assertEqual(report.total, euros("120.00"))

    def test_a_debit_with_one_invoice_of_several_articles_is_split_between_them(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "75.00"), (self.wine, "25.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Bières": euros("90.00"), "Vins": euros("30.00")})

    def test_a_debit_paying_several_invoices_adds_their_categories_up(self):
        first = self.invoice(self.goods, date(2026, 6, 8), (self.beer, "50.00"))
        second = self.invoice(self.goods, date(2026, 6, 9), (self.wine, "50.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), first, second)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Bières": euros("60.00"), "Vins": euros("60.00")})
        self.assertEqual(report.total, euros("120.00"))

    def test_a_charge_goes_to_its_supplier_and_not_to_an_article_category(self):
        """A charge has no article, so there is no article category it could
        belong to. Its supplier IS what it is - the rent, the electricity."""
        rent = self.invoice(self.landlord, date(2026, 6, 1))
        rent.reconciliation_adjustment = euros("900.00")
        rent.save(update_fields=["reconciliation_adjustment"])
        self.pay(self.debit(date(2026, 6, 5), "BAILLEUR EXEMPLE", "900.00"), rent)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Bailleur Exemple": euros("900.00")})

    def test_a_goods_line_no_article_claims_is_said_and_not_guessed(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (None, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {computation.TO_CLASSIFY_NAME: euros("120.00")})

    def test_the_places_are_the_ones_the_margins_page_reads(self):
        """One definition of « where an invoice's money went », not two."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "75.00"), (self.wine, "25.00"))
        places = computation.where_it_went(document)
        self.assertEqual(
            sum((money.ttc for money in places.values()), Decimal("0")),
            computation.cents(document.total_ttc),
        )
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(
            sorted(self.amounts(report).values()),
            sorted(money.ttc for money in places.values()),
        )


class WhenTheInvoicesDoNotAddUpTests(SpendingFixtures, TestCase):
    def test_a_debit_bigger_than_its_invoice_leaves_the_difference_uninvoiced(self):
        """80,00 € of invoice under a 120,00 € debit: 40,00 € left the
        account with nothing to show for it, and that is not the beer's."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "66.67"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(
            self.amounts(report),
            {"Bières": euros("80.00"), spending.NO_CATEGORY: euros("40.00")},
        )
        self.assertEqual(report.total, euros("120.00"))
        self.assertEqual(report.uninvoiced, euros("40.00"))

    def test_the_difference_is_unsaid_even_where_the_line_carries_a_category(self):
        """The line says « Divers » and its invoice covers 80,00 € of 120,00 €.
        The 40,00 € nothing invoiced is « Sans catégorie » all the same.

        It used to take « Divers », and « Divers 40,00 € » then sat in the pie
        with nothing on the page able to explain it or correct it: the list
        below only offers the lines with **no** invoice at all, so that line
        is not in it, and « Divers » carries no reconciling figure. « Sans
        catégorie » is the one label the page reconciles
        (`unsaid_beyond_the_list`) and the one whose stat points at where the
        missing invoice is found.
        """
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "66.67"))
        line = self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00", category="Divers")
        self.pay(line, document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(
            self.amounts(report),
            {"Bières": euros("80.00"), spending.NO_CATEGORY: euros("40.00")},
        )
        self.assertEqual(report.unsaid_beyond_the_list, euros("40.00"))

    def test_a_rules_category_does_not_name_what_a_linked_debit_overpaid(self):
        """Same rule, same silence: a rule is a guess about a whole line with
        no document, not about the part of a linked one nothing explains."""
        IgnoreRule.objects.create(pattern="GROSSISTE", description="Prêt", category="Emprunt")
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "50.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(
            self.amounts(report),
            {"Bières": euros("60.00"), spending.NO_CATEGORY: euros("60.00")},
        )
        self.assertNotIn("Emprunt", self.amounts(report))

    def test_an_invoice_costing_more_than_the_debit_is_scaled_to_what_left(self):
        """An invoice settled in two goes: each line counts what IT paid.
        Counted whole on both, the beer would be bought twice."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "70.00"), document)
        self.pay(self.debit(date(2026, 6, 20), "GROSSISTE EXEMPLE", "50.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Bières": euros("120.00")})
        self.assertEqual(report.total, euros("120.00"))

    def test_a_scaled_invoice_still_adds_up_to_the_debit_to_the_cent(self):
        """Three places scaled by 1/3 round to a centime over; the remainder
        goes on the largest, so the line counts exactly what left."""
        document = self.invoice(
            self.goods,
            date(2026, 6, 10),
            (self.beer, "0.01"),
            (self.wine, "0.01"),
            (None, "0.01"),
        )
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "0.01"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(report.total, euros("0.01"))
        self.assertEqual(sum(self.amounts(report).values()), euros("0.01"))

    def test_the_rounding_remainder_is_put_back_rather_than_lost(self):
        """Three places of 12,00 € under a 10,00 € debit: a third of each is
        3,3333…, which rounds to 3,33 three times and leaves 9,99. The
        centime that is missing goes on the largest place, or the column
        does not add up to the statement - and « les catégories font le
        relevé au centime près » is the one promise this page lives on.

        The 0,01 € case above never exercises this: its three places round
        to 0,01 / 0,00 / 0,00, which already adds up, so the remainder it is
        named after is zero.
        """
        document = self.invoice(
            self.goods,
            date(2026, 6, 10),
            (self.beer, "10.00"),
            (self.wine, "10.00"),
            (None, "10.00"),
        )
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "10.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual(sorted(self.amounts(report).values()), [euros("3.33"), euros("3.33"), euros("3.34")])
        self.assertEqual(sum(self.amounts(report).values()), report.total)
        self.assertEqual(report.total, euros("10.00"))


class ByHandAndByRuleTests(SpendingFixtures, TestCase):
    def test_a_debit_with_no_invoice_takes_the_category_typed_on_it(self):
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "300.00", category="Travaux")
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Travaux": euros("300.00")})
        self.assertEqual([category.by_rule for category in report.categories], [0])

    def test_a_rule_categorises_the_payments_it_recognises(self):
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt", category="Emprunt")
        self.debit(date(2026, 6, 5), "PRET EXEMPLE", "450.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Emprunt": euros("450.00")})
        category = report.categories[0]
        self.assertEqual((category.operations, category.by_rule), (1, 1))

    def test_what_a_person_typed_beats_what_a_rule_says(self):
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt", category="Emprunt")
        self.debit(date(2026, 6, 5), "PRET EXEMPLE", "450.00", category="Emprunt bancaire")
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {"Emprunt bancaire": euros("450.00")})
        self.assertEqual(report.categories[0].by_rule, 0)

    def test_a_rule_with_no_category_leaves_the_payment_uncategorised(self):
        """« Pas de facture attendue » says there is nothing to link, not
        what the money was for."""
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt")
        self.debit(date(2026, 6, 5), "PRET EXEMPLE", "450.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {spending.NO_CATEGORY: euros("450.00")})

    def test_a_suspended_rule_categorises_nothing(self):
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt", category="Emprunt", is_active=False)
        self.debit(date(2026, 6, 5), "PRET EXEMPLE", "450.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report), {spending.NO_CATEGORY: euros("450.00")})


class UncategorisedTests(SpendingFixtures, TestCase):
    def test_a_debit_nobody_categorised_is_listed_and_counted_first(self):
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "10.00")
        self.debit(date(2026, 6, 4), "AUTRE PAYEUR", "500.00", category="Travaux")
        report = spending.spending_for(WINDOW)
        self.assertEqual(report.categories[0].name, spending.NO_CATEGORY)
        self.assertEqual(report.categories[0].amount, euros("10.00"))
        self.assertEqual([one.line.counterparty for one in report.uncategorised], ["PAYEUR EXEMPLE"])

    def test_a_debit_an_invoice_explains_is_not_in_the_list_to_categorise(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        report = spending.spending_for(WINDOW)
        self.assertEqual((report.uncategorised, report.without_invoice), ([], []))

    def test_the_uncategorised_are_the_biggest_first(self):
        self.debit(date(2026, 6, 3), "PETIT PAYEUR", "10.00")
        self.debit(date(2026, 6, 4), "GROS PAYEUR", "800.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual([one.line.counterparty for one in report.uncategorised], ["GROS PAYEUR", "PETIT PAYEUR"])

    def test_a_spending_already_named_stays_listed_so_a_typo_can_be_retyped(self):
        """Named once and gone from the page, a category typed by mistake
        would have nowhere left to be corrected."""
        self.debit(date(2026, 6, 3), "PETIT PAYEUR", "10.00")
        self.debit(date(2026, 6, 4), "GROS PAYEUR", "800.00", category="Travo")
        report = spending.spending_for(WINDOW)
        # The unnamed first: that is the work, whatever the amounts.
        self.assertEqual(
            [(one.line.counterparty, one.name) for one in report.without_invoice],
            [("PETIT PAYEUR", spending.NO_CATEGORY), ("GROS PAYEUR", "Travo")],
        )

    def test_the_uncategorised_row_and_the_list_below_it_are_reconciled(self):
        """The row holds the part of a debit its invoice did not cover; the
        list below holds the debits with no invoice at all. Two figures under
        one label, and the page has to say what separates them."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "66.67"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        self.debit(date(2026, 6, 13), "PAYEUR EXEMPLE", "10.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(report.unsaid_total, euros("50.00"))
        self.assertEqual(report.uncategorised_total, euros("10.00"))
        self.assertEqual(report.unsaid_beyond_the_list, euros("40.00"))


class TheWindowTests(SpendingFixtures, TestCase):
    def test_only_what_left_the_account_inside_the_window_counts(self):
        self.debit(date(2026, 5, 31), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        self.debit(date(2026, 6, 1), "PAYEUR EXEMPLE", "10.00", category="Travaux")
        self.debit(date(2026, 6, 30), "PAYEUR EXEMPLE", "5.00", category="Travaux")
        self.debit(date(2026, 7, 1), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        report = spending.spending_for(WINDOW)
        self.assertEqual(report.total, euros("15.00"))

    def test_an_empty_window_has_no_categories_and_no_pie(self):
        self.debit(date(2026, 7, 5), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        report = spending.spending_for(WINDOW)
        self.assertEqual((report.total, report.categories, report.slices), (Decimal("0"), [], []))
        self.assertEqual(report.operations, 0)

    def test_income_is_not_spending_and_is_in_none_of_it(self):
        self.income(date(2026, 6, 4), "ENCAISSEMENT EXEMPLE", "2000.00")
        self.debit(date(2026, 6, 5), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        report = spending.spending_for(WINDOW)
        self.assertEqual(report.total, euros("100.00"))
        self.assertEqual(report.operations, 1)

    def test_a_window_with_only_income_draws_nothing_rather_than_dividing_by_it(self):
        self.income(date(2026, 6, 4), "ENCAISSEMENT EXEMPLE", "2000.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual((report.total, report.categories, report.slices), (Decimal("0"), [], []))


class ThePieTests(SpendingFixtures, TestCase):
    def slices(self, report) -> dict:
        return {piece.name: piece.amount for piece in report.slices}

    def test_the_slices_add_up_to_what_the_pie_says_it_draws(self):
        self.debit(date(2026, 6, 3), "A", "300.00", category="Travaux")
        self.debit(date(2026, 6, 4), "B", "100.00", category="Assurance")
        self.debit(date(2026, 6, 5), "C", "50.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(sum(piece.amount for piece in report.slices), report.drawn_total)
        self.assertEqual(report.drawn_total, report.total)
        self.assertEqual(
            sum(piece.share for piece in report.slices).quantize(Decimal("0.01")),
            Decimal("100.00"),
        )

    def test_the_categories_add_up_to_what_left_the_account(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "145.30"), document)
        self.debit(date(2026, 6, 13), "PAYEUR EXEMPLE", "7.83")
        report = spending.spending_for(WINDOW)
        self.assertEqual(sum(self.amounts(report).values()), report.total)
        self.assertEqual(report.total, euros("153.13"))

    def test_the_biggest_come_first_and_nothing_readable_is_folded(self):
        """Nine categories of about a ninth each. Every one is big enough to
        name, so the pie draws nine wedges and « Autres » does not exist at
        all - the rule used to keep as many wedges as the palette had
        colours, which buried an eighth of the money under a heading nobody
        can look up (owner, 24/09)."""
        for index in range(9):
            self.debit(date(2026, 6, 3), f"PAYEUR {index}", 100 - index, category=f"Poste {index}")
        report = spending.spending_for(WINDOW)
        self.assertEqual(len(report.slices), 9)
        self.assertNotIn(spending.OTHERS, [piece.name for piece in report.slices])
        self.assertEqual([piece.name for piece in report.slices][:2], ["Poste 0", "Poste 1"])
        # The table still holds every one of them: the pie is a shape, the
        # table is the figures.
        self.assertEqual(len(report.categories), 9)

    def test_half_a_per_cent_is_the_bar_and_it_is_not_crossed(self):
        """990, 5, 3 and 2 of a thousand: 99,0 %, 0,5 %, 0,3 % and 0,2 %.
        The one AT the bar keeps its wedge - the page prints « 0.5 % » beside
        it, and a wedge printed at half a per cent that sits inside
        « Autres » is a legend arguing with its own table."""
        for payee, amount, name in (
            ("PAYEUR A", "990.00", "Gros poste"),
            ("PAYEUR B", "5.00", "Poste au seuil"),
            ("PAYEUR C", "3.00", "Petit poste"),
            ("PAYEUR D", "2.00", "Tout petit poste"),
        ):
            self.debit(date(2026, 6, 3), payee, amount, category=name)
        report = spending.spending_for(WINDOW)
        self.assertEqual(
            [piece.name for piece in report.slices],
            ["Gros poste", "Poste au seuil", spending.OTHERS],
        )
        others = report.slices[-1]
        self.assertEqual((others.held, others.amount), (2, euros("5.00")))
        self.assertEqual(sum(piece.share for piece in report.slices), Decimal("100"))
        # And the table still names the two it folded.
        self.assertIn("Tout petit poste", [one.name for one in report.categories])

    def test_one_category_alone_below_the_bar_is_named_rather_than_folded(self):
        """« Autres (1 catégorie) » is a wedge that says strictly less than
        the name it replaced, at exactly the same size."""
        self.debit(date(2026, 6, 3), "PAYEUR A", "997.00", category="Gros poste")
        self.debit(date(2026, 6, 3), "PAYEUR B", "3.00", category="Petit poste")
        report = spending.spending_for(WINDOW)
        self.assertEqual([piece.name for piece in report.slices], ["Gros poste", "Petit poste"])

    def test_the_last_wedge_never_takes_the_first_one_s_colour(self):
        """A pie closes on itself, so its last wedge touches its first. Past
        the palette's length the cycle would draw the two alike, and two
        neighbours of one colour read as one wedge."""
        for index in range(len(PIE_COLORS) + 1):
            self.debit(date(2026, 6, 3), f"PAYEUR {index}", "100.00", category=f"Poste {index:02d}")
        report = spending.spending_for(WINDOW)
        self.assertEqual(len(report.slices), len(PIE_COLORS) + 1)
        self.assertNotEqual(report.slices[-1].color, report.slices[0].color)

    def test_uncategorised_keeps_its_own_slice_however_small(self):
        """Thinner than the bar and still its own wedge: it is the work the
        page exists to get rid of, and folded away nobody would see it."""
        self.debit(date(2026, 6, 3), "PAYEUR A", "990.00", category="Gros poste")
        for index, amount in enumerate(("3.00", "2.00")):
            self.debit(date(2026, 6, 3), f"PAYEUR {index}", amount, category=f"Petit {index}")
        self.debit(date(2026, 6, 4), "PAYEUR X", "0.50")
        report = spending.spending_for(WINDOW)
        names = [piece.name for piece in report.slices]
        self.assertEqual(names[0], spending.NO_CATEGORY)
        self.assertLess(report.slices[0].share, spending.SMALLEST_SLICE)
        self.assertIn(spending.OTHERS, names)

    def test_the_shares_add_up_to_a_hundred_per_cent_as_the_page_prints_them(self):
        """Three equal categories: an unrounded third prints « 33.3 % » three
        times, and a reader adding the legend gets 99,9 % of a pie that is by
        construction the whole of what was drawn. The rounding goes back on
        the largest, exactly as `_scaled` puts a centime back."""
        for name in ("Aaa inventé", "Bbb inventé", "Ccc inventé"):
            self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category=name)
        report = spending.spending_for(WINDOW)
        shares = [category.share for category in report.categories]
        self.assertEqual(sum(shares), Decimal("100"))
        self.assertEqual(sorted(shares), [Decimal("33.3"), Decimal("33.3"), Decimal("33.4")])
        self.assertEqual(sum(piece.share for piece in report.slices), Decimal("100"))

    def test_the_tail_folded_into_the_others_slice_carries_the_shares_it_holds(self):
        """« Autres » is the sum of its members' shares, not a second
        division: otherwise the legend and the table beside it print two
        different figures for the same money, and the wedges stop coming to
        a hundred."""
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "900.00", category="Gros poste")
        for index in range(3):
            self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", f"{3 - index}.00", category=f"Poste {index}")
        report = spending.spending_for(WINDOW)
        others = report.slices[-1]
        self.assertEqual(others.name, spending.OTHERS)
        drawn = {category.name: category.share for category in report.categories}
        named = {piece.name for piece in report.slices[:-1]}
        self.assertEqual(others.share, sum(share for name, share in drawn.items() if name not in named))
        self.assertEqual(sum(piece.share for piece in report.slices), Decimal("100"))

    def test_a_category_given_back_more_than_it_cost_is_out_of_the_pie_and_said(self):
        """A returned deposit can leave a category negative over a month.
        A negative slice cannot be drawn, so it is listed instead - never
        dropped, and the account's own total stays what left it."""
        credit = self.invoice(self.goods, date(2026, 6, 10), (self.wine, "-50.00"))
        goods = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "60.00"), credit, goods)
        report = spending.spending_for(WINDOW)
        self.assertEqual(self.amounts(report)["Vins"], euros("-60.00"))
        self.assertNotIn("Vins", [piece.name for piece in report.slices])
        self.assertEqual(report.given_back, euros("-60.00"))
        self.assertEqual(report.total, euros("60.00"))
        self.assertEqual(sum(piece.amount for piece in report.slices), report.drawn_total)


class ThePageTests(SpendingFixtures, TestCase):
    def test_the_page_says_what_it_counts_and_points_at_the_margins_page(self):
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertEqual(page.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, page, "dépenses")
        self.assertContains(page, "sorti du compte")
        self.assertContains(page, reverse("margins:margins_home"))
        self.assertContains(page, "Travaux")
        self.assertContains(page, "100.00")

    def test_with_no_dates_the_page_shows_the_last_twelve_months_and_names_them(self):
        today = timezone.localdate()
        self.debit(today - timedelta(days=10), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        self.debit(today - timedelta(days=400), "PAYEUR EXEMPLE", "999.00", category="Travaux")
        page = self.client.get(self.url)
        self.assertContains(page, "douze derniers mois")
        self.assertContains(page, "100.00")
        self.assertNotContains(page, "999.00")

    def test_everything_is_shown_when_the_page_is_asked_for_all_of_it(self):
        today = timezone.localdate()
        self.debit(today - timedelta(days=400), "PAYEUR EXEMPLE", "999.00", category="Travaux")
        page = self.client.get(self.url, {"tout": "1"})
        self.assertContains(page, "999.00")

    def test_a_date_that_is_no_date_is_no_window_rather_than_a_500(self):
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        page = self.client.get(self.url, {"du": "hier", "au": "2026-02-30"})
        self.assertEqual(page.status_code, 200)

    def test_the_pie_is_drawn_as_an_inline_svg_with_its_figures_beside_it(self):
        self.debit(date(2026, 6, 3), "A", "300.00", category="Travaux")
        self.debit(date(2026, 6, 4), "B", "100.00", category="Assurance")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "<svg")
        self.assertContains(page, "chart-slice")
        self.assertContains(page, "75.0 %")
        self.assertContains(page, "300.00")

    def test_a_line_categorised_by_a_rule_says_so_rather_than_passing_for_a_decision(self):
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt", category="Emprunt")
        self.debit(date(2026, 6, 5), "PRET EXEMPLE", "450.00")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "par règle")

    def test_the_pies_tail_says_how_many_categories_it_holds(self):
        """A wedge called « Autres » that does not say what it stands for is
        a wedge nobody can account for."""
        self.debit(date(2026, 6, 3), "PAYEUR GROS", "990.00", category="Gros poste")
        for index, amount in enumerate(("3.00", "2.00")):
            self.debit(date(2026, 6, 3), f"PAYEUR {index}", amount, category=f"Poste {index}")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "Autres (2 catégories)")
        # And each of them is still a row of its own in the table.
        self.assertContains(page, "Poste 1")

    def test_the_headline_says_what_separates_it_from_the_list_below(self):
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "66.67"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        self.debit(date(2026, 6, 13), "PAYEUR EXEMPLE", "10.00")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "50.00")
        self.assertContains(page, "40.00 € débités au-delà des factures rattachées")


class SettingACategoryTests(SpendingFixtures, TestCase):
    def post(self, line, value, **extra):
        return self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "category", "categorie": value, **extra},
            follow=True,
        )

    def test_typing_a_category_files_the_spending_under_it(self):
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00")
        self.post(line, "Travaux")
        line.refresh_from_db()
        self.assertEqual(line.category, "Travaux")
        self.assertEqual(self.amounts(spending.spending_for(WINDOW)), {"Travaux": euros("100.00")})

    def test_the_page_comes_back_on_the_same_window(self):
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00")
        back = f"{self.url}?du=2026-06-01&au=2026-06-30"
        answer = self.post(line, "Travaux", next=back)
        self.assertEqual(answer.redirect_chain[-1][0], back)

    def test_categorising_does_not_settle_the_line_by_hand(self):
        """What the money was for says nothing about whether the invoice is
        still to be found: left as a decision, the automatic pass would never
        look at this line again."""
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00")
        self.post(line, "Travaux")
        line.refresh_from_db()
        self.assertFalse(line.settled_by_hand)

    def test_an_empty_category_takes_the_spending_back_to_uncategorised(self):
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        self.post(line, "   ")
        line.refresh_from_db()
        self.assertEqual(line.category, "")
        self.assertEqual(self.amounts(spending.spending_for(WINDOW)), {spending.NO_CATEGORY: euros("100.00")})

    def test_a_category_wider_than_its_column_is_cut_rather_than_stored_whole(self):
        """SQLite stores an over-long string without a word; every read of it
        afterwards is Django's problem and the owner's."""
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00")
        self.post(line, "T" * 400)
        line.refresh_from_db()
        self.assertEqual(line.category, "T" * spending.CATEGORY_MAX)

    def test_a_category_carrying_a_control_character_is_a_page_not_a_traceback(self):
        """SQLite refuses a NUL in a string outright, so a tampered form
        reached the owner as « A string literal cannot contain NUL »."""
        line = self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00")
        self.post(line, "Tra\x00vaux\r\n  divers\t")
        line.refresh_from_db()
        self.assertEqual(line.category, "Travaux divers")

    def test_a_category_on_a_line_that_is_no_id_is_a_message_not_a_500(self):
        answer = self.client.post(
            reverse("bank:bank_line_action", args=[999999]), {"action": "category", "categorie": "X"}
        )
        self.assertEqual(answer.status_code, 404)

    def test_the_operations_column_says_it_is_not_a_total(self):
        """One debit whose invoice spans two article categories draws two
        rows saying « 1 operation » under a foot saying « 1 »: both figures
        are right and one under the other they read as a subtraction error.
        The header and a sentence say the same line can be in several
        categories - on the one page whose argument is that it adds up."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "50.00"), (self.wine, "50.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "120.00"), document)
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "Opérations concernées")
        self.assertContains(page, "plusieurs catégories")

    def test_what_came_back_on_the_statement_is_not_claimed_by_the_deduction_stat(self):
        """`given_back` is the credit notes and returnable deposits the LINKED
        invoices carry, not money the bank paid back: a credit is income and
        this page counts what went out. Named « Rendu sur la période », the
        stat promised a figure it does not hold."""
        document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "-100.00"))
        self.pay(self.debit(date(2026, 6, 12), "GROSSISTE EXEMPLE", "50.00"), document)
        self.debit(date(2026, 6, 13), "PAYEUR EXEMPLE", "200.00", category="Travaux")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "Déduit par les factures")
        self.assertNotContains(page, "Rendu sur la période")
        self.assertContains(page, "est pas compté ici")

    def test_the_work_list_is_capped_and_says_how_many_it_left_out(self):
        """Every row is a form with a text field in it, and over « tout
        l'historique » a multi-year statement draws them all. The cap says
        what it cut - and the figures above it still count every one, which
        is what the sentence has to promise."""
        for index in range(spending.LIST_SIZE + 4):
            self.debit(date(2026, 6, 3), f"PAYEUR {index}", "10.00")
        report = spending.spending_for(WINDOW)
        self.assertEqual(len(report.listed), spending.LIST_SIZE)
        self.assertEqual(report.not_listed, 4)
        self.assertEqual(report.uncategorised_total, euros("640.00"))
        self.assertEqual(report.unsaid_total, report.uncategorised_total)
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "4 dépenses sans facture de plus")
        self.assertEqual(page.content.decode().count('name="categorie"'), spending.LIST_SIZE)

    def test_a_category_typed_by_hand_is_text_the_table_can_find_and_sort(self):
        """The cell is an `<input>`, and the table's own search and sort read
        textContent: a line already named « Loyer » could not be found by
        typing « Loyer », while one a rule named could, and a click on the
        header sorted every row on the empty string."""
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category="Loyer inventé")
        page = self.client.get(self.url, WINDOW.parameters)
        body = page.content.decode()
        self.assertIn('data-sort="Loyer inventé"', body)
        self.assertIn("« Loyer inventé »", body)

    def test_the_form_offers_the_categories_that_already_exist(self):
        self.debit(date(2026, 6, 3), "PAYEUR EXEMPLE", "100.00", category="Travaux")
        self.debit(date(2026, 6, 4), "AUTRE PAYEUR", "50.00")
        IgnoreRule.objects.create(pattern="PRET EXEMPLE", description="Prêt", category="Emprunt")
        page = self.client.get(self.url, WINDOW.parameters)
        self.assertContains(page, "Travaux")
        self.assertContains(page, "Emprunt")
        self.assertContains(page, "Bières")


class QueryCountTests(SpendingFixtures, TestCase):
    def build(self, count):
        for index in range(count):
            document = self.invoice(self.goods, date(2026, 6, 10), (self.beer, "10.00"), (self.wine, "5.00"))
            self.pay(self.debit(date(2026, 6, 12), f"GROSSISTE {index}", "18.00"), document)
            self.debit(date(2026, 6, 13), f"AUTRE {index}", "3.00")

    def test_three_times_the_operations_cost_no_more_queries(self):
        self.build(2)
        with self.assertNumQueries(spending.QUERIES) as small:
            spending.spending_for(WINDOW)
        self.build(4)
        with self.assertNumQueries(len(small.captured_queries)):
            spending.spending_for(WINDOW)
