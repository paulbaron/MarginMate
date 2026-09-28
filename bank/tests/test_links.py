"""A bank line and its invoices, linked however they really are.

What a person may do here, and what the page owes them in return:

* link an invoice **whatever it costs** - the bank's figure and the
  document's disagree far more often than they match, and a person who knows
  why must be able to say so;
* link **several invoices to one line** (one debit for two deliveries) and
  the **same invoice to several lines** (an invoice settled in two goes);
* see the **gap**, never hidden: both figures and the difference, so a line
  « rapprochée » that does not add up says so on its own row;
* take **one** invoice off a line and leave the others.

And one thing the automatic pass owes them: it stays out of all of it. It
proposes and links an invoice nothing pays yet, and nothing else - a second
line taking an invoice already settled is a decision only a person takes.

Every figure below is invented.
"""

import re
from datetime import date
from decimal import Decimal
from html import unescape

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from bank import reconcile
from bank.models import BankTransaction, CounterpartyAlias, InvoicePayment
from bank.tests.test_reconcile import Fixtures, debit_row
from invoices.deletion import delete_invoice
from tests.factories import make_supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

#: An invented wholesaler and the payee its bank lines carry. This
#: repository is public: a supplier or a bank payee the owner really deals
#: with never goes into a test, however ordinary the name looks.
WHOLESALER = "GROSSISTE"
PAYEE = "GROSSISTE INVENTE"

HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


def _forms(response):
    return re.findall(r"<form[^>]*>.*?</form>", response.content.decode(), re.DOTALL)


def _hidden_fields(form: str) -> dict:
    """A form's hidden fields as the browser would send them back.

    Unescaped: an attribute the page writes « &amp; » in is posted « & », so
    a test reading the attribute raw asserts something no browser ever sends.
    """
    return {name: unescape(value) for name, value in HIDDEN.findall(form)}


def search_form_of(response, line) -> dict:
    """The hidden fields of the search box on `line`'s own row.

    Found by the row's own id rather than by position: every row carries a
    search box, and the first one on the page belongs to whichever line the
    tab happens to sort first.
    """
    for form in _forms(response):
        fields = _hidden_fields(form)
        if 'name="recherche"' in form and fields.get("ligne") == str(line.pk):
            return fields
    raise AssertionError(f"no search box on the row of line {line.pk}")


class LinkPage(Fixtures):
    """Two invoices of one wholesaler adding up to a debit, one that does
    not, and an invoice big enough to need two debits.

    The wholesaler is made here rather than taken from the seeded suppliers:
    this repository is public, and a supplier the owner really buys from has
    no business being written into a test.
    """

    def setUp(self):
        self.url = reverse("bank:bank_home")
        make_supplier(code=WHOLESALER, name="Grossiste Inventé")
        # 72,00 € and 48,00 € TTC: together, exactly the 120,00 € debit.
        self.first = self.invoice(WHOLESALER, date(2026, 6, 29), "60.00", invoice_number="F-0001")
        self.second = self.invoice(WHOLESALER, date(2026, 7, 2), "40.00", invoice_number="F-0002")
        # 60,00 € TTC: with the first, 12,00 € more than the debit.
        self.third = self.invoice(WHOLESALER, date(2026, 7, 3), "50.00", invoice_number="F-0003")
        self.load(
            debit_row(date(2026, 7, 9), PAYEE, "120,00"),
            debit_row(date(2026, 7, 16), PAYEE, "60,00"),
            debit_row(date(2026, 7, 20), "BAILLEUR EXEMPLE", "900,00"),
        )

    def line(self, counterparty, amount=None):
        lines = BankTransaction.objects.filter(counterparty=counterparty)
        return lines.get(amount=Decimal(amount)) if amount is not None else lines.get()

    def act(self, line, action, **data):
        return self.client.post(reverse("bank:bank_line_action", args=[line.pk]), {"action": action, **data})

    def linked(self, **filters):
        return self.client.get(self.url, {"vue": "rapprochees", **filters})

    def invoices_of(self, line):
        return set(line.payments.values_list("invoice__invoice_number", flat=True))


class SeveralInvoicesOnOneLineTests(LinkPage, TestCase):
    def test_two_invoices_adding_up_to_the_debit_are_linked_together(self):
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk, self.second.pk])
        self.assertEqual(self.invoices_of(debit), {"F-0001", "F-0002"})
        page = self.linked()
        self.assertContains(page, "F-0001")
        self.assertContains(page, "F-0002")
        # Nothing to say: the invoices are exactly what left the account.
        self.assertNotContains(page, "de plus que la dépense")
        self.assertNotContains(page, "de moins que la dépense")

    def test_invoices_worth_more_than_the_debit_say_so_on_the_row(self):
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk, self.third.pk])
        page = self.linked()
        # Both figures and the difference: a person who linked them knows
        # why, and a person reading the page a month later does not.
        self.assertContains(page, "132.00")
        self.assertContains(page, "120.00")
        self.assertContains(page, "12.00 € de plus que la dépense")

    def test_an_invoice_worth_less_than_the_debit_says_so_too(self):
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk])
        page = self.linked()
        self.assertContains(page, "72.00")
        self.assertContains(page, "48.00 € de moins que la dépense")

    def test_a_line_that_does_not_add_up_is_rapprochee_all_the_same(self):
        """A person said this debit paid that invoice. The figure is what
        tells them it is not the whole story - not a tab hiding the line."""
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk])
        debit.refresh_from_db()
        self.assertTrue(debit.settled_by_hand)
        self.assertEqual(self.client.get(self.url, {"vue": "rapprochees"}).context["stats"]["linked_count"], 1)
        self.assertEqual(self.client.get(self.url).context["stats"]["todo_count"], 2)


class OneInvoiceOnSeveralLinesTests(LinkPage, TestCase):
    def setUp(self):
        super().setUp()
        # 180,00 € TTC, settled by a 120,00 € debit and a 60,00 € one.
        self.big = self.invoice(WHOLESALER, date(2026, 6, 20), "150.00", invoice_number="F-0010")
        self.small_debit = self.line(PAYEE, "-60.00")
        self.big_debit = self.line(PAYEE, "-120.00")

    def link_both(self):
        self.act(self.big_debit, "link", invoice=[self.big.pk])
        self.act(self.small_debit, "link", invoice=[self.big.pk])

    def test_the_same_invoice_is_linked_to_two_lines(self):
        self.link_both()
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.big).count(), 2)
        self.assertEqual(self.invoices_of(self.big_debit), {"F-0010"})
        self.assertEqual(self.invoices_of(self.small_debit), {"F-0010"})

    def test_each_row_says_the_invoice_is_settled_by_another_line_too(self):
        self.link_both()
        page = self.linked()
        self.assertContains(page, "Aussi réglée par", count=2)
        self.assertContains(page, "16/07/2026")
        self.assertContains(page, "09/07/2026")

    def test_the_invoice_page_says_how_many_operations_paid_it(self):
        self.link_both()
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.big.pk]))
        self.assertContains(page, "2 opérations")
        self.assertContains(page, "09/07/2026")
        self.assertContains(page, "16/07/2026")
        assertNoUnrenderedTemplateSyntax(self, page, "invoice_detail")

    def test_one_operation_still_reads_as_one(self):
        """One 120,00 € debit on a 180,00 € invoice - and the page says the
        60,00 € still outstanding rather than « Payée ».

        A link no longer has to add up, so this IS the first of two
        instalments; the second may never arrive, and the invoice has left
        `reconcile.unpaid_invoices` for ever - no pass and no pick-list will
        raise it again. This page is where a person comes to find out
        whether a document was settled.
        """
        self.act(self.big_debit, "link", invoice=[self.big.pk])
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.big.pk]))
        self.assertContains(page, "Réglée en partie")
        self.assertContains(page, "120.00 € réglés sur 180.00 €")
        self.assertNotContains(page, "opérations")

    def test_an_invoice_its_operations_add_up_to_reads_payee(self):
        """« Payée » is kept for when the two agree - it is the word that
        means « nothing left to look for »."""
        self.link_both()
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.big.pk]))
        self.assertContains(page, "Payée")
        self.assertNotContains(page, "réglés sur")

    def test_the_same_pair_is_never_stored_twice(self):
        self.act(self.big_debit, "link", invoice=[self.big.pk])
        self.act(self.big_debit, "link", invoice=[self.big.pk])
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.big).count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InvoicePayment.objects.create(
                transaction=self.big_debit, invoice=self.big, method=InvoicePayment.Method.MANUAL
            )


class SearchTests(LinkPage, TestCase):
    """Beside the suggestions: any invoice at all, found by supplier, number,
    date or amount - `invoices.workspace.documents_matching`."""

    def setUp(self):
        super().setUp()
        # Six months before the rent debit and at another amount: no window
        # reaches it, so no suggestion and no pick list ever offers it.
        self.old = self.invoice(WHOLESALER, date(2026, 1, 5), "20.00", invoice_number="F-0091")
        self.rent = self.line("BAILLEUR EXEMPLE")

    def search(self, line, text, **filters):
        return self.client.get(self.url, {"ligne": line.pk, "recherche": text, **filters})

    def test_an_invoice_no_suggestion_offers_is_found_and_linked(self):
        offered = self.client.get(self.url)
        self.assertNotContains(offered, "F-0091")
        found = self.search(self.rent, "F-0091")
        self.assertContains(found, "F-0091")
        self.assertContains(found, f'value="{self.old.pk}"')
        self.act(self.rent, "link", invoice=[self.old.pk])
        self.assertEqual(self.invoices_of(self.rent), {"F-0091"})

    def test_an_invoice_already_paid_is_offered_and_marked(self):
        self.act(self.line(PAYEE, "-120.00"), "link", invoice=[self.first.pk])
        found = self.search(self.rent, "F-0001")
        self.assertContains(found, "F-0001")
        self.assertContains(found, "Déjà réglée par")
        self.assertContains(found, "09/07/2026")

    def test_several_found_invoices_are_linked_at_once(self):
        self.act(self.rent, "link", invoice=[self.first.pk, self.old.pk])
        self.assertEqual(self.invoices_of(self.rent), {"F-0001", "F-0091"})

    def test_an_invoice_this_line_already_pays_is_not_offered_again(self):
        self.act(self.rent, "link", invoice=[self.old.pk])
        found = self.search(self.rent, "F-0091", vue="rapprochees")
        self.assertContains(found, "déjà rattachée à cette opération")
        self.assertNotContains(found, f'name="invoice" value="{self.old.pk}"')

    def test_nothing_found_says_so_and_keeps_the_search(self):
        found = self.search(self.rent, "F-9999")
        self.assertContains(found, "Aucun document")
        self.assertContains(found, "F-9999")

    def test_the_search_carries_the_window_and_the_tab(self):
        found = self.search(self.rent, "F-0091", vue="toutes", du="2026-07-01", au="2026-07-31")
        self.assertContains(found, "F-0091")
        self.assertEqual(found.context["view"], "toutes")
        self.assertEqual(found.context["date_window"].start, date(2026, 7, 1))

    def test_a_line_id_that_is_no_id_is_no_search(self):
        page = self.client.get(self.url, {"ligne": "; DROP", "recherche": "F-0091"})
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "F-0091")

    def test_the_search_box_sends_the_window_back_the_way_a_browser_would(self):
        """A GET form posts the fields it carries and NOTHING else. So what
        is asserted here is the box the page really drew, resubmitted field
        by field - not a query string this test wished for.

        Without « du »/« au » among them, searching a document from a row
        under a window would answer on the whole statement: the list, the
        four figures and the six tab counts all widening at once, with
        nothing on screen saying the dates were dropped.
        """
        window = {"vue": "toutes", "du": "2026-07-01", "au": "2026-07-31"}
        fields = search_form_of(self.client.get(self.url, window), self.rent)
        self.assertEqual({key: fields.get(key) for key in window}, window)

        answer = self.client.get(self.url, {**fields, "recherche": "F-0091"})

        self.assertContains(answer, "F-0091")
        self.assertEqual(answer.context["view"], "toutes")
        self.assertEqual(answer.context["date_window"].start, date(2026, 7, 1))
        self.assertEqual(answer.context["date_window"].end, date(2026, 7, 31))

    def test_linking_what_the_search_found_comes_back_without_the_search(self):
        """« next » carries the tab and the window and deliberately not the
        row's search: left on, the page would come back still showing one
        line's search results over a statement the reader has moved on from.
        """
        window = {"vue": "toutes", "du": "2026-07-01", "au": "2026-07-31"}
        page = self.client.get(self.url, {**window, "ligne": self.rent.pk, "recherche": "F-0091"})
        back = {
            fields["next"]
            for fields in (_hidden_fields(form) for form in _forms(page))
            if fields.get("action") == "link" and "next" in fields
        }

        self.assertTrue(back, "the form linking what the search found carries no « next »")
        for target in back:
            self.assertIn("vue=toutes", target)
            self.assertIn("du=2026-07-01", target)
            self.assertIn("au=2026-07-31", target)
            self.assertNotIn("recherche", target)
            self.assertNotIn("ligne", target)

    def test_ticking_nothing_links_nothing_and_says_so(self):
        """The result list is a row of checkboxes, and a browser sends an
        unticked box as nothing at all: « Rattacher » with none of them on
        arrives as a POST with no invoice in it."""
        answer = self.act(self.rent, "link")

        self.assertFalse(self.rent.payments.exists())
        self.assertContains(self.client.get(answer.url), "Choisissez la facture")

    def test_a_document_deleted_since_the_page_was_drawn_is_left_out(self):
        """A page left open while the document was deleted elsewhere. The
        invoices that are still there are linked; the id that names nothing
        is dropped rather than raising."""
        gone = self.third.pk
        delete_invoice(self.third)

        self.act(self.rent, "link", invoice=[self.first.pk, gone])

        self.assertEqual(self.invoices_of(self.rent), {"F-0001"})

    def test_a_page_whose_every_document_is_gone_is_a_message(self):
        gone = self.third.pk
        delete_invoice(self.third)

        answer = self.act(self.rent, "link", invoice=[gone])

        self.assertFalse(self.rent.payments.exists())
        self.assertContains(self.client.get(answer.url), "Choisissez la facture")


class AutomaticPassTests(LinkPage, TestCase):
    def test_an_invoice_that_has_a_payment_is_never_linked_again(self):
        """Only a person links an invoice twice, deliberately. Left to the
        automatic pass, the next debit of the same amount would quietly pay
        an invoice already settled - and every figure would still add up."""
        # F-0003 is 60,00 € TTC, exactly the second debit, and a person has
        # just put it on the first one.
        self.act(self.line(PAYEE, "-120.00"), "link", invoice=[self.third.pk])
        self.assertEqual(reconcile.reconcile(), 0)
        self.assertEqual(InvoicePayment.objects.count(), 1)
        # An invoice nothing pays, at that same amount: linked, so it really
        # was the payment that held the pass back.
        self.invoice(WHOLESALER, date(2026, 7, 14), "50.00", invoice_number="F-0004")
        self.assertEqual(reconcile.reconcile(), 1)
        self.assertEqual(self.invoices_of(self.line(PAYEE, "-60.00")), {"F-0004"})
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.third).count(), 1)

    def test_an_invoice_paid_by_a_line_is_no_longer_suggested(self):
        self.act(self.line(PAYEE, "-120.00"), "link", invoice=[self.third.pk])
        self.assertNotIn(self.third.pk, [invoice.pk for invoice in reconcile.unpaid_invoices()])


class UnlinkTests(LinkPage, TestCase):
    def setUp(self):
        super().setUp()
        self.debit = self.line(PAYEE, "-120.00")
        self.act(self.debit, "link", invoice=[self.first.pk, self.second.pk, self.third.pk])

    def test_one_invoice_is_taken_off_and_the_others_stay(self):
        self.act(self.debit, "unlink_invoice", invoice=self.second.pk)
        self.assertEqual(self.invoices_of(self.debit), {"F-0001", "F-0003"})
        self.debit.refresh_from_db()
        self.assertTrue(self.debit.settled_by_hand)

    def test_the_gap_follows_what_is_left(self):
        self.act(self.debit, "unlink_invoice", invoice=self.third.pk)
        self.assertContains(self.linked(), "120.00")
        self.assertNotContains(self.linked(), "de plus que la dépense")

    def test_the_whole_line_is_unlinked_in_one_go(self):
        self.act(self.debit, "unlink")
        self.assertFalse(self.debit.payments.exists())

    def test_taking_the_last_invoice_off_leaves_the_line_settled_by_hand(self):
        for invoice in (self.first, self.second, self.third):
            self.act(self.debit, "unlink_invoice", invoice=invoice.pk)
        self.debit.refresh_from_db()
        self.assertFalse(self.debit.payments.exists())
        self.assertTrue(self.debit.settled_by_hand)
        # The automatic pass may link the other lines; this one it leaves
        # alone for good, the way an « unlink » of the whole line does.
        reconcile.reconcile()
        self.assertFalse(self.debit.payments.exists())

    def test_an_invoice_this_line_does_not_pay_is_said(self):
        other = self.line("BAILLEUR EXEMPLE")
        response = self.act(other, "unlink_invoice", invoice=self.first.pk)
        self.assertEqual(self.invoices_of(self.debit), {"F-0001", "F-0002", "F-0003"})
        self.assertContains(self.client.get(response.url), "ne paie pas")

    def test_an_invoice_id_that_is_no_id_is_a_message_not_a_500(self):
        response = self.act(self.debit, "unlink_invoice", invoice="²")
        self.assertEqual(self.invoices_of(self.debit), {"F-0001", "F-0002", "F-0003"})
        self.assertContains(self.client.get(response.url), "Choisissez la facture")

    def test_pas_de_facture_attendue_still_clears_every_link(self):
        self.act(self.debit, "no_invoice")
        self.debit.refresh_from_db()
        self.assertFalse(self.debit.payments.exists())
        self.assertTrue(self.debit.no_invoice)
        self.assertEqual(self.client.get(self.url, {"vue": "sans-facture"}).context["stats"]["no_invoice_count"], 1)


class DeletedDocumentTests(LinkPage, TestCase):
    def test_a_document_deleted_while_linked_leaves_the_others_and_the_gap(self):
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk, self.second.pk])
        delete_invoice(self.second)
        self.assertEqual(self.invoices_of(debit), {"F-0001"})
        page = self.linked()
        self.assertContains(page, "48.00 € de moins que la dépense")
        assertNoUnrenderedTemplateSyntax(self, page, "rapprochees")

    def test_a_document_deleted_from_two_lines_frees_both(self):
        big = self.invoice(WHOLESALER, date(2026, 6, 20), "150.00", invoice_number="F-0010")
        for amount in ("-120.00", "-60.00"):
            self.act(self.line(PAYEE, amount), "link", invoice=[big.pk])
        delete_invoice(big)
        self.assertFalse(InvoicePayment.objects.exists())
        # Both lines stay settled by hand: a person linked them, and the
        # automatic pass is not told otherwise by a deletion.
        self.assertEqual(reconcile.open_lines().filter(settled_by_hand=False).count(), 1)


class FiguresTests(LinkPage, TestCase):
    """The four figures and the six tab counts, with the links a person may
    now make: one line paying two invoices, one invoice paid by two lines."""

    def test_they_still_add_up(self):
        big = self.invoice(WHOLESALER, date(2026, 6, 20), "150.00", invoice_number="F-0010")
        self.act(self.line(PAYEE, "-120.00"), "link", invoice=[self.first.pk, self.second.pk, big.pk])
        self.act(self.line(PAYEE, "-60.00"), "link", invoice=[big.pk])
        self.act(self.line("BAILLEUR EXEMPLE"), "no_invoice")

        stats = self.client.get(self.url, {"vue": "toutes"}).context["stats"]
        self.assertEqual(stats["spending_count"], 3)
        self.assertEqual(stats["spending_total"], Decimal("1080.00"))
        self.assertEqual((stats["linked_count"], stats["linked_total"]), (2, Decimal("180.00")))
        self.assertEqual((stats["todo_count"], stats["todo_total"]), (0, Decimal("0")))
        self.assertEqual((stats["no_invoice_count"], stats["no_invoice_total"]), (1, Decimal("900.00")))

        counts = stats["counts"]
        self.assertEqual(counts["a-traiter"] + counts["rapprochees"] + counts["sans-facture"], counts["toutes"])
        self.assertEqual(counts["toutes"], stats["spending_count"])
        self.assertEqual(counts["entrees"], 0)
        # Four payments over two lines: a line counts once, not once per
        # invoice, or « Rattachées » would read as more operations than the
        # statement holds.
        self.assertEqual(InvoicePayment.objects.count(), 4)


class WhatALinkTeachesTests(LinkPage, TestCase):
    """Linking by hand records a `CounterpartyAlias`, and the automatic pass
    acts on it next month without asking anybody.

    Nothing on any page shows an alias, so what teaches one has to be a link
    the pass itself could have made. A person may now link ANY document at
    ANY amount - that is what « Chercher une facture » is for - and a link
    that does not add up is evidence about one debit, not about a name.
    """

    #: A payee whose words name no supplier here - an alias is only ever
    #: recorded for one the matching could not have recognised on its own.
    UNKNOWN_PAYEE = "SUMUP LE COMPTOIR"

    def setUp(self):
        super().setUp()
        # 120,00 €, exactly what `first` and `second` come to together.
        self.load(debit_row(date(2026, 7, 11), self.UNKNOWN_PAYEE, "120,00"))
        self.unknown = self.line(self.UNKNOWN_PAYEE)

    def aliases(self):
        return set(CounterpartyAlias.objects.values_list("name", flat=True))

    def test_a_link_that_adds_up_teaches_the_payee(self):
        """Two invoices making exactly the debit: the bank really does print
        this payee for that supplier, and next month's payment links on its
        own."""
        self.act(self.unknown, "link", invoice=[self.first.pk, self.second.pk])
        self.assertEqual(self.aliases(), {self.UNKNOWN_PAYEE})

    def test_a_link_at_another_amount_teaches_nothing(self):
        """The same payee linked to one 72,00 € invoice under a 120,00 €
        debit - the mis-tick « Chercher une facture » makes possible, since
        it offers every document in the table at any amount and any date.
        Taught from it, next month's payment to this payee would quietly
        settle another of that supplier's invoices."""
        self.act(self.unknown, "link", invoice=[self.first.pk])
        self.assertEqual(self.aliases(), set())

    def test_a_debit_settled_by_two_invoices_linked_one_at_a_time_still_teaches(self):
        """The second link is what makes the line add up, and it teaches
        exactly as much as linking both at once: measured on everything the
        line pays, not on the invoices this click added."""
        self.act(self.unknown, "link", invoice=[self.first.pk])
        self.assertEqual(self.aliases(), set())
        self.act(self.unknown, "link", invoice=[self.second.pk])
        self.assertEqual(self.aliases(), {self.UNKNOWN_PAYEE})


class IncomeLineTests(LinkPage, TestCase):
    def setUp(self):
        super().setUp()
        self.money_in = BankTransaction.objects.create(
            operation_date=date(2026, 7, 10),
            label="VIREMENT RECU CLIENT INVENTE",
            counterparty="CLIENT INVENTE",
            amount=Decimal("500.00"),
            kind=BankTransaction.Kind.TRANSFER,
            fingerprint="income-invented-1",
        )

    def test_an_income_line_never_settles_an_invoice(self):
        """No form is drawn on an income row, so this is a stale or a crafted
        POST. Linked, the invoice would leave `unpaid_invoices` for ever and
        read « Payée » on its own page, while showing on no bank tab and in
        no figure of « Dépenses », which counts debits only."""
        answer = self.act(self.money_in, "link", invoice=[self.first.pk])
        self.assertFalse(self.money_in.payments.exists())
        self.assertContains(self.client.get(answer.url), "entrée d&#x27;argent ne règle pas")


class SecondInvoiceOnALinkedLineTests(LinkPage, TestCase):
    """One debit for two deliveries, the second document added afterwards."""

    def test_a_linked_row_still_offers_the_unpaid_invoices_at_those_dates(self):
        """The pick-list is the control that needs no knowledge; the search
        needs a word the reader has to know. Losing the list the moment the
        first invoice goes on left the second one reachable only by typing."""
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk])
        page = self.linked()
        self.assertContains(page, "Rattacher une facture de plus")
        self.assertContains(page, f'<option value="{self.second.pk}">')

    def test_the_second_invoice_is_linked_from_that_list(self):
        """Linked with the id the page really offered, not one this test
        wished for: a list the page no longer draws is a link nobody can
        make."""
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk])
        offered = re.findall(r'<option value="(\d+)">', self.linked().content.decode())
        self.assertIn(str(self.second.pk), offered)

        self.act(debit, "link", invoice=[self.second.pk])

        self.assertEqual(self.invoices_of(debit), {"F-0001", "F-0002"})
        self.assertNotContains(self.linked(), "de moins que la dépense")

    def test_an_invoice_already_on_the_line_is_not_offered_there(self):
        """`unpaid_invoices` is « no payment at all », so what this line
        already pays cannot come back in its own list."""
        debit = self.line(PAYEE, "-120.00")
        self.act(debit, "link", invoice=[self.first.pk])
        page = self.linked()
        self.assertContains(page, "Rattacher une facture de plus")
        self.assertNotContains(page, f'<option value="{self.first.pk}">')


class ListsThatWereCutInSilenceTests(LinkPage, TestCase):
    """A list cut with nothing said reads as « the document is not there »,
    and the reader stops looking. Both caps say how many they left out."""

    def test_the_search_says_how_many_documents_it_did_not_show(self):
        from bank.views import MAX_FOUND

        for number in range(MAX_FOUND + 3):
            self.invoice(WHOLESALER, date(2026, 5, 2), "10.00", invoice_number=f"F-90{number:02d}")
        found = self.client.get(self.url, {"ligne": self.line(PAYEE, "-120.00").pk, "recherche": "F-90"})
        self.assertContains(found, "3 documents de plus")

    def test_the_pick_list_says_how_many_invoices_it_did_not_show(self):
        from bank.views import MAX_CHOICES

        for number in range(MAX_CHOICES + 2):
            self.invoice(WHOLESALER, date(2026, 7, 5), "10.00", invoice_number=f"F-80{number:02d}")
        page = self.client.get(self.url)
        self.assertContains(page, "de plus à ces dates")
