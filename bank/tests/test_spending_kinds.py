"""« Dépenses »: seeing the spending classified by hand.

A debit with no invoice gets its category from one of three places - typed on
the line, carried by an ignore rule, or nowhere - and the page prints which on
each row. What it had no way to do was SHOW one of the three: the work list is
capped at `LIST_SIZE` and sorted unnamed-first, so on a real statement the
hand-typed debits sat behind more unnamed ones than the cap draws and not one
of them was reachable, at any period, by any amount of scrolling (read-only
measurement: over a year, a few hand-typed debits among hundreds with no
invoice, and every row the page drew was unnamed).

So the filter narrows the list BEFORE the cap. Applied after it, « classées à
la main » would draw an empty table under a chip counting them.

Two things it must not do: move a single figure above the list - those are read
off the whole window, and a page whose argument is that its categories add up
to what left the account must not appear to change the money when a work list
is narrowed - and lose the period it was chosen under.

Every name and figure below is invented.
"""

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bank.models import BankTransaction, IgnoreRule, InvoicePayment
from bank.spending import BY_HAND, BY_RULE, EMPTY, LIST_SIZE, UNSAID, spending_for
from common import DateRange
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier

WINDOW = DateRange(start=date(2026, 1, 1), end=date(2026, 12, 31))


class Fixtures:
    def setUp(self):
        self.url = reverse("bank:spending_home")

    def debit(self, amount, label="PRLV EXEMPLE", day=date(2026, 3, 2), category=""):
        return BankTransaction.objects.create(
            operation_date=day,
            amount=-Decimal(amount),
            label=label,
            counterparty=label.split()[-1],
            category=category,
            fingerprint=f"fp-{label}-{amount}-{day}-{category}",
        )

    def rule(self, pattern, category):
        return IgnoreRule.objects.create(pattern=pattern, category=category, description=pattern, is_active=True)

    def report(self, kind=""):
        return spending_for(WINDOW, kind=kind)

    def names(self, kind=""):
        return [one.line.label for one in self.report(kind).listed]


class WhichKindTests(Fixtures, TestCase):
    def test_a_line_carries_how_it_was_named(self):
        self.rule("REGLE", "Loyer")
        typed = self.debit("10.00", label="PRLV MAIN", category="Matériel")
        ruled = self.debit("20.00", label="PRLV REGLE")
        silent = self.debit("30.00", label="PRLV RIEN")
        by_line = {one.line.pk: one for one in self.report().without_invoice}
        self.assertEqual(by_line[typed.pk].source, BY_HAND)
        self.assertEqual(by_line[ruled.pk].source, BY_RULE)
        self.assertEqual(by_line[silent.pk].source, UNSAID)
        self.assertTrue(by_line[typed.pk].by_hand)
        self.assertFalse(by_line[ruled.pk].by_hand)
        self.assertFalse(by_line[silent.pk].by_hand)

    def test_each_kind_is_counted_over_the_whole_window_not_the_page(self):
        """The chip's figure is what there IS, never what fitted."""
        self.rule("REGLE", "Loyer")
        for number in range(LIST_SIZE + 5):
            self.debit("1.00", label=f"PRLV RIEN {number}")
        self.debit("10.00", label="PRLV MAIN", category="Matériel")
        self.debit("20.00", label="PRLV REGLE")
        counts = self.report().counts
        self.assertEqual(counts[UNSAID], LIST_SIZE + 5)
        self.assertEqual(counts[BY_HAND], 1)
        self.assertEqual(counts[BY_RULE], 1)
        self.assertEqual(counts[""], LIST_SIZE + 7)


class FilterBeforeTheCapTests(Fixtures, TestCase):
    def test_a_hand_typed_line_past_the_cap_is_still_shown(self):
        """The regression that matters. Unnamed lines sort first, so on the
        real statement every hand-typed debit sat past the cap and the
        page could not show one at all."""
        for number in range(LIST_SIZE + 10):
            self.debit("1.00", label=f"PRLV RIEN {number}")
        self.debit("10.00", label="PRLV MAIN", category="Matériel")
        # Unfiltered it is nowhere to be seen...
        self.assertNotIn("PRLV MAIN", self.names())
        # ...and that is exactly what the filter is for.
        self.assertEqual(self.names(BY_HAND), ["PRLV MAIN"])

    def test_the_cap_still_applies_inside_a_filter(self):
        for number in range(LIST_SIZE + 5):
            self.debit("1.00", label=f"PRLV MAIN {number}", category="Matériel")
        report = self.report(BY_HAND)
        self.assertEqual(len(report.listed), LIST_SIZE)
        self.assertEqual(report.not_listed, 5)

    def test_what_the_cap_leaves_out_is_counted_inside_the_filter(self):
        """« 5 de plus » has to mean five more OF THIS VIEW, not five more
        of a list the reader is not looking at."""
        for number in range(LIST_SIZE + 5):
            self.debit("1.00", label=f"PRLV MAIN {number}", category="Matériel")
        for number in range(20):
            self.debit("1.00", label=f"PRLV RIEN {number}")
        self.assertEqual(self.report(BY_HAND).not_listed, 5)
        self.assertEqual(self.report(UNSAID).not_listed, 0)


class TheFiguresDoNotMoveTests(Fixtures, TestCase):
    """Narrowing the work list must not touch one figure above it."""

    def setUp(self):
        super().setUp()
        self.rule("REGLE", "Loyer")
        self.debit("10.00", label="PRLV MAIN", category="Matériel")
        self.debit("20.00", label="PRLV REGLE")
        self.debit("30.00", label="PRLV RIEN")

    def test_the_total_and_the_categories_are_the_same_under_every_filter(self):
        whole = self.report()
        for kind in (BY_HAND, BY_RULE, UNSAID):
            with self.subTest(kind=kind):
                narrowed = self.report(kind)
                self.assertEqual(narrowed.total, whole.total)
                self.assertEqual(narrowed.operations, whole.operations)
                self.assertEqual(
                    [(one.name, one.amount) for one in narrowed.categories],
                    [(one.name, one.amount) for one in whole.categories],
                )
                self.assertEqual(narrowed.drawn_total, whole.drawn_total)
                self.assertEqual(narrowed.unsaid_total, whole.unsaid_total)
                self.assertEqual(narrowed.uncategorised_total, whole.uncategorised_total)

    def test_the_slices_of_the_pie_are_the_same_under_every_filter(self):
        whole = [(one.name, one.amount, one.share) for one in self.report().slices]
        for kind in (BY_HAND, BY_RULE, UNSAID):
            with self.subTest(kind=kind):
                self.assertEqual([(one.name, one.amount, one.share) for one in self.report(kind).slices], whole)


class ItCostsNothingTests(Fixtures, TestCase):
    """`spending.QUERIES` is pinned over `spending_for` itself, and the list
    is read afterwards through lazy properties - so the pin does not reach
    them. Filtering and counting are plain Python over the list already
    built; asked of the database instead, they would be one query per debit
    of the year, which is the N+1 that pin exists for.
    """

    def test_reading_every_kind_costs_no_query_at_all(self):
        self.rule("REGLE", "Loyer")
        for number in range(30):
            self.debit("1.00", label=f"PRLV RIEN {number}")
            self.debit("2.00", label=f"PRLV MAIN {number}", category="Matériel")
            self.debit("3.00", label=f"PRLV REGLE {number}")
        for kind in ("", BY_HAND, BY_RULE, UNSAID):
            with self.subTest(kind=kind):
                report = spending_for(WINDOW, kind=kind)
                with self.assertNumQueries(0):
                    report.counts  # noqa: B018 - read on purpose, the pin counts its queries
                    list(report.selected)
                    list(report.listed)
                    report.not_listed  # noqa: B018 - read on purpose, the pin counts its queries

    def test_a_filtered_report_costs_exactly_what_an_unfiltered_one_costs(self):
        """Pinned against the unfiltered call on the same data rather than
        against `QUERIES` itself: that constant is the whole path including
        the invoices, and a fixture with no paid debit never reaches its
        fourth query. What must hold is that choosing a kind adds none."""
        self.rule("REGLE", "Loyer")
        for number in range(10):
            self.debit("2.00", label=f"PRLV MAIN {number}", category="Matériel")
            self.debit("3.00", label=f"PRLV REGLE {number}")
        with CaptureQueriesContext(connection) as whole:
            spending_for(WINDOW)
        with self.assertNumQueries(len(whole.captured_queries)):
            spending_for(WINDOW, kind=BY_HAND)


class ThePageTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.rule("REGLE", "Loyer")
        self.debit("10.00", label="PRLV MAIN", category="Matériel")
        self.debit("20.00", label="PRLV REGLE")
        self.debit("30.00", label="PRLV RIEN")

    def page(self, **params):
        return self.client.get(self.url, {"du": "2026-01-01", "au": "2026-12-31", **params})

    def shown(self, response):
        return [one.line.label for one in response.context["report"].listed]

    def test_the_page_draws_a_chip_for_each_kind(self):
        chips = {chip["key"]: chip for chip in self.page().context["kind_chips"]}
        self.assertEqual(set(chips), {"", UNSAID, BY_RULE, BY_HAND})
        self.assertEqual(chips[BY_HAND]["count"], 1)
        self.assertEqual(chips[""]["count"], 3)

    def test_choosing_a_kind_narrows_the_list(self):
        self.assertEqual(self.shown(self.page(classement=BY_HAND)), ["PRLV MAIN"])
        self.assertEqual(self.shown(self.page(classement=BY_RULE)), ["PRLV REGLE"])
        self.assertEqual(sorted(self.shown(self.page())), ["PRLV MAIN", "PRLV REGLE", "PRLV RIEN"])

    def test_every_chip_keeps_the_period(self):
        for chip in self.page().context["kind_chips"]:
            with self.subTest(chip=chip["key"]):
                self.assertIn("du=2026-01-01", chip["url"])
                self.assertIn("au=2026-12-31", chip["url"])

    def test_a_chip_keeps_all_time_too(self):
        for chip in self.page(tout="1").context["kind_chips"]:
            with self.subTest(chip=chip["key"]):
                self.assertIn("tout=1", chip["url"])

    def test_classing_a_line_comes_back_to_the_same_view(self):
        """Typed from inside « classées par règle », the answer must not be
        the unfiltered page two screens further up."""
        response = self.page(classement=BY_RULE)
        self.assertIn(f"classement={BY_RULE}", response.context["page_url"])

    def classify(self, line, value, came_from):
        return self.client.post(
            reverse("bank:bank_line_action", args=[line.pk]),
            {"action": "category", "categorie": value, "next": came_from},
            follow=True,
        )

    def test_classing_a_line_says_which_list_it_moves_to(self):
        """From « À classer » a named line leaves that list at once. Said,
        or it vanishes from under the pointer with nothing explaining it."""
        line = BankTransaction.objects.get(label="PRLV RIEN")
        answer = self.classify(line, "Travaux", f"{self.url}?classement={UNSAID}")
        said = " ".join(str(m) for m in answer.context["messages"])
        self.assertIn("Travaux", said)
        self.assertIn("Classées à la main", said)

    def test_it_says_nothing_when_the_line_stays_where_it_was(self):
        line = BankTransaction.objects.get(label="PRLV MAIN")
        answer = self.classify(line, "Autre chose", f"{self.url}?classement={BY_HAND}")
        said = " ".join(str(m) for m in answer.context["messages"])
        self.assertIn("Autre chose", said)
        self.assertNotIn("Elle passe", said)

    def test_it_says_nothing_from_the_unfiltered_page(self):
        line = BankTransaction.objects.get(label="PRLV RIEN")
        answer = self.classify(line, "Travaux", self.url)
        self.assertNotIn("Elle passe", " ".join(str(m) for m in answer.context["messages"]))

    def test_a_line_a_rule_still_names_moves_back_to_by_rule(self):
        """Clearing a typed category does not make a line unnamed when a
        rule names it too - the message must not say it went to « À classer »."""
        line = BankTransaction.objects.get(label="PRLV REGLE")
        line.category = "Autre chose"
        line.save(update_fields=["category"])
        answer = self.classify(line, "", f"{self.url}?classement={BY_HAND}")
        self.assertIn("Classées par règle", " ".join(str(m) for m in answer.context["messages"]))

    def test_a_kind_nobody_can_use_is_the_whole_list(self):
        """It arrives from a query string, so a stale bookmark and a typed
        URL both land here - an empty page under a filter the reader cannot
        see reads as a page that has broken."""
        for value in ("abc", "MAIN", "1", "regle ", "<script>"):
            with self.subTest(value=value):
                response = self.page(classement=value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(sorted(self.shown(response)), ["PRLV MAIN", "PRLV REGLE", "PRLV RIEN"])
                self.assertEqual(response.context["kind"], "")

    def test_an_empty_view_says_so_rather_than_drawing_nothing(self):
        BankTransaction.objects.filter(category="Matériel").update(category="")
        response = self.page(classement=BY_HAND)
        self.assertEqual(self.shown(response), [])
        self.assertContains(response, "Aucune dépense classée à la main")

    def test_each_empty_view_says_which_one_is_empty(self):
        """The sentence comes from spending.EMPTY, never from the template
        branching on a kind's stored value: spelled out in HTML, the day one
        of those values changes the page would tell a reader looking at
        « Classées à la main » that there is nothing left to classify."""
        BankTransaction.objects.all().delete()
        self.rule("REGLE", "Loyer")
        self.debit("20.00", label="PRLV REGLE")
        for kind in (BY_HAND, UNSAID):
            with self.subTest(kind=kind):
                self.assertContains(self.page(classement=kind), EMPTY[kind])

    def test_a_line_with_an_invoice_is_in_no_kind(self):
        """The list is the debits NOTHING invoiced; a filter does not widen
        it."""
        supplier = make_supplier(code="GROSSISTE", name="Grossiste Inventé")
        invoice = make_invoice(supplier=supplier, invoice_date=date(2026, 3, 1))
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=supplier), total_ht="10.00", vat_rate=Decimal("0.20")
        )
        paid = self.debit("12.00", label="PRLV FACTUREE", category="Matériel")
        InvoicePayment.objects.create(transaction=paid, invoice=invoice, method=InvoicePayment.Method.MANUAL)
        self.assertNotIn("PRLV FACTUREE", self.names(BY_HAND))
        self.assertEqual(self.report().counts[BY_HAND], 1)
