"""« Propositions » (/banque/propositions/): every open line the matching has
a suggestion for, by tier, accepted in one POST.

What the page owes the reader: the near-sure ones ticked and the rule they
were ticked by stated; the rest unticked, a radio per option where there are
several and none chosen for them; and a POST that trusts nothing it is
handed - a line gone, an income line, an invoice paid meanwhile, an option
the matching does not offer are each skipped and SAID. Every figure and
every supplier name here is invented; the suppliers are the ones the rest of
the tests already use.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from bank import matching, reconcile
from bank.models import BankTransaction, CounterpartyAlias, InvoicePayment
from bank.tests.test_reconcile import FIVE_FIVE, Fixtures, card_row, debit_row
from tests.factories import make_supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

SUBSCRIPTION = "ABONNEMENT"
SUBSCRIPTION_PAYEE = "ABONNEMENT EXEMPLE SAS"

CHECKBOX = re.compile(r'<input type="checkbox" name="ligne" value="(\d+)"[^>]*>', re.DOTALL)


def checkboxes(response) -> dict[int, bool]:
    """Each line's checkbox on the page, and whether it is ticked."""
    return {int(pk): "checked" in tag for tag, pk in ((m.group(0), m.group(1)) for m in CHECKBOX.finditer(response.content.decode()))}


def radios(response, line) -> list[str]:
    """The radio tags of one line's options, as rendered."""
    return re.findall(rf'<input type="radio" name="option-{line.pk}"[^>]*>', response.content.decode(), re.DOTALL)


class ProposalsPage(Fixtures):
    """One near-sure recurring debit (three identical monthly invoices, the
    month's four days before the debit), one unnamed card payment (a single
    option, to confirm), one debit with two identical invoices a week apart
    (two options, to confirm), one rent with nothing to propose, and one
    entry of money."""

    def setUp(self):
        self.url = reverse("bank:proposals")
        self.post_url = reverse("bank:link_proposals")
        make_supplier(code=SUBSCRIPTION, name="Abonnement Exemple")
        # 23,70 € TTC each (19,75 € HT at 20 %), dated the 2nd of each month.
        self.may, self.june, self.july = (
            self.invoice(SUBSCRIPTION, date(2026, month, 2), "19.75", invoice_number=f"A-{month:02d}") for month in (5, 6, 7)
        )
        self.receipt = self.invoice("MONOPRIX", date(2026, 7, 15), "12.38", rate=FIVE_FIVE, invoice_number="T-1")
        # 126,00 € TTC each (105,00 € HT at 20 %).
        self.first = self.invoice("METRO", date(2026, 6, 20), "105.00", invoice_number="M-1")
        self.second = self.invoice("METRO", date(2026, 6, 25), "105.00", invoice_number="M-2")
        self.load(
            debit_row(date(2026, 7, 6), SUBSCRIPTION_PAYEE, "23,70"),
            card_row(date(2026, 7, 15), "PAYTERM *EPICERIE 12", "13,06"),
            debit_row(date(2026, 7, 1), "METRO FRANCE", "126,00"),
            debit_row(date(2026, 7, 20), "BAILLEUR EXEMPLE", "900,00"),
        )
        self.income = BankTransaction.objects.create(
            operation_date=date(2026, 7, 2), amount=Decimal("50.00"), label="VIR RECU EXEMPLE", fingerprint="in-1"
        )
        reconcile.reconcile()
        self.subscription = self.line(SUBSCRIPTION_PAYEE)
        self.card = self.line("PAYTERM *EPICERIE 12")
        self.metro = self.line("METRO FRANCE")
        self.rent = self.line("BAILLEUR EXEMPLE")

    def line(self, counterparty):
        return BankTransaction.objects.get(counterparty=counterparty)

    def accept(self, **data):
        return self.client.post(self.post_url, data, follow=True)

    def messages_of(self, response) -> str:
        return " ".join(str(message) for message in response.context["messages"])

    def rent_invoice(self):
        """A landlord and the month's rent invoice, 900,00 € TTC: with the
        rent line already on the statement and the pass not run again, that
        is a SURE match the page has to show."""
        make_supplier(code="BAILLEUR", name="Bailleur Exemple")
        return self.invoice("BAILLEUR", date(2026, 7, 1), "750.00", invoice_number="L-07")

    def group_order(self, response, line) -> int:
        """Which group a line's checkbox sits in: 0 « Certaines », 1
        « Quasi-sûres », 2 « À confirmer » - the nearest group title above
        the checkbox (the rules card names the tiers too, higher up, but not
        as group titles). A group with no rows has no title, so counting the
        titles above would read « À confirmer » as « Quasi-sûres » on a page
        with nothing near-sure."""
        content = response.content.decode()
        position = content.index(f'name="ligne" value="{line.pk}"')
        groups = ("Certaines", "Quasi-sûres", "À confirmer")
        above = [
            (found.start(), groups.index(found.group(1)))
            for found in re.finditer(r'class="proposal-group-title">\s*(Certaines|Quasi-sûres|À confirmer)', content)
            if found.start() < position
        ]
        return max(above)[1]


class ProposalsPageTests(ProposalsPage, TestCase):
    def test_nothing_was_linked_automatically(self):
        """The fixture is three questions: the page has something to show."""
        self.assertFalse(InvoicePayment.objects.exists())

    def test_the_page_renders_and_states_every_tier_s_rule(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, "propositions")
        for _tier, label, rule in matching.TIER_RULES:
            self.assertContains(response, label)
            # As the template writes it: an apostrophe is escaped on the way.
            self.assertContains(response, escape(rule))
        self.assertContains(response, f"{matching.RECURRING_DAYS_BEFORE.days} jours")
        self.assertContains(response, "data-table")
        # The rent has nothing proposed, and the page says so rather than
        # reading as « everything is done ».
        self.assertContains(response, "1 dépense sans facture n'a aucune")

    def test_near_sure_rows_are_ticked_and_the_others_are_not(self):
        ticked = checkboxes(self.client.get(self.url))
        self.assertEqual(ticked, {self.subscription.pk: True, self.card.pk: False, self.metro.pk: False})
        self.assertNotIn(self.rent.pk, ticked)
        self.assertNotIn(self.income.pk, ticked)

    def test_a_multi_option_row_has_a_radio_per_option_and_none_chosen(self):
        response = self.client.get(self.url)
        metro_radios = radios(response, self.metro)
        self.assertEqual(len(metro_radios), 2)
        self.assertFalse(any("checked" in tag for tag in metro_radios))
        # The near-sure recurring debit lists its three months, the month's
        # first and chosen.
        subscription_radios = radios(response, self.subscription)
        self.assertEqual(len(subscription_radios), 3)
        self.assertIn("checked", subscription_radios[0])
        self.assertIn(f'value="{self.july.pk}"', subscription_radios[0])
        self.assertFalse(any("checked" in tag for tag in subscription_radios[1:]))
        # A single option needs no radio: it travels as a hidden field.
        self.assertEqual(radios(response, self.card), [])
        self.assertContains(response, f'<input type="hidden" name="option-{self.card.pk}" value="{self.receipt.pk}">')

    def test_each_row_says_its_reason_and_its_tier_s_reason(self):
        response = self.client.get(self.url)
        self.assertContains(response, "4 jours avant le paiement")
        self.assertContains(response, "ne nomme pas ce fournisseur")
        self.assertContains(response, "rien ne les départage")
        self.assertContains(response, "quasi-sûre")
        self.assertContains(response, "à confirmer")

    def test_the_bank_page_shows_the_tier_on_its_rows_too(self):
        """The pill is something the application worked out, so the row
        says the tier's reason under it and the pill leads to the rules."""
        response = self.client.get(reverse("bank:bank_home"))
        rules = reverse("bank:proposals") + "#regles"
        self.assertContains(response, f'href="{rules}" class="status-pill status-tier-near_sure">quasi-sûre')
        self.assertContains(response, f'href="{rules}" class="status-pill status-tier-to_confirm">à confirmer')
        self.assertContains(response, "4 jours avant le paiement")
        self.assertContains(response, "ne nomme pas ce fournisseur : à vous de le reconnaître")
        self.assertContains(response, reverse("bank:proposals"))
        # And the anchor exists where the pill sends the reader.
        self.assertContains(self.client.get(self.url), 'id="regles"')

    def test_a_line_a_person_unlinked_is_offered_but_not_ticked(self):
        reconcile.unlink(self.subscription)
        response = self.client.get(self.url)
        self.assertEqual(checkboxes(response)[self.subscription.pk], False)
        self.assertContains(response, "Déliée à la main")
        self.assertFalse(any("checked" in tag for tag in radios(response, self.subscription)))

    def test_a_sure_line_the_pass_has_not_seen_yet_has_its_own_group_and_is_ticked(self):
        """The flow the page is for: statement imported (the pass ran), then
        the invoices, and « Relancer le rapprochement » not clicked. The rent
        is now a SURE match; it is not « à confirmer », and the page says
        why it is here at all. Failing first: it sat unticked under « À
        confirmer » with a « certaine » pill, while the rules card said such
        a line never appears."""
        rent_invoice = self.rent_invoice()
        response = self.client.get(self.url)
        self.assertEqual(checkboxes(response)[self.rent.pk], True)
        self.assertEqual(self.group_order(response, self.rent), 0)
        self.assertEqual(self.group_order(response, self.subscription), 1)
        self.assertEqual(self.group_order(response, self.card), 2)
        self.assertContains(response, "Certaines")
        self.assertContains(response, "n&#x27;a pas été relancé depuis le dernier import")
        self.assertContains(response, 'status-tier-sure">certaine')
        # The sure rent and the near-sure subscription; the two to confirm
        # stay unticked.
        self.assertContains(response, "2 sélectionnées")
        accepted = self.accept(ligne=[self.rent.pk], **{f"option-{self.rent.pk}": str(rent_invoice.pk)})
        self.assertIn("1 proposition rattachée", self.messages_of(accepted))
        payment = InvoicePayment.objects.get(transaction=self.rent)
        self.assertEqual((payment.invoice, payment.method), (rent_invoice, InvoicePayment.Method.MANUAL))

    def test_a_sure_line_a_person_unlinked_stays_unticked_with_its_note(self):
        rent_invoice = self.rent_invoice()
        reconcile.link(self.rent, [rent_invoice])
        reconcile.unlink(self.rent)
        response = self.client.get(self.url)
        self.assertEqual(checkboxes(response)[self.rent.pk], False)
        self.assertEqual(self.group_order(response, self.rent), 0)
        self.assertContains(response, "Déliée à la main")
        self.assertContains(response, "1 sélectionnée")
        # The group's own heading says the exception too - the only row in
        # it is unticked, and a heading reading « cochées d'avance » alone
        # contradicted the row under it.
        self.assertContains(response, "sauf celles déliées à la main")

    def test_a_sure_line_reaching_months_back_is_shown_certaine_but_not_ticked(self):
        """The cascade the bulk page makes routine: a supplier debited
        monthly, whose invoices at one figure were all linked in one go, and
        one more debit whose month's invoice is at ANOTHER figure. The one
        unpaid invoice left at the debit's amount is five months old. For
        the matching it is SURE - one exact, named, within the 180 days the
        pass reaches - and « Relancer le rapprochement » would link it; this
        stream does not move that rule. What the page owes the reader is the
        distance, said, and no pre-tick. Failing first: it came back ticked
        « Certaine » under « le rapprochement n'a pas été relancé », with no
        word about the months."""
        make_supplier(code="ALARME", name="Alarme Exemple")
        paid = date(2026, 7, 24)
        far = matching.RECURRING_DAYS_BEFORE.days + 125
        # 31,20 € TTC (26,00 € HT) five months back; the month's at 44,40 €.
        old = self.invoice("ALARME", paid - timedelta(days=far), "26.00", invoice_number="S-02")
        self.invoice("ALARME", paid - timedelta(days=10), "37.00", invoice_number="S-07")
        self.load(debit_row(paid, "ALARME EXEMPLE", "31,20"))
        alarm = self.line("ALARME EXEMPLE")
        response = self.client.get(self.url)
        self.assertEqual(checkboxes(response)[alarm.pk], False)
        self.assertEqual(self.group_order(response, alarm), 0)
        self.assertContains(response, 'status-tier-sure">certaine')
        self.assertContains(response, f"datée {far} jours avant le paiement")
        self.assertContains(response, f"au-delà de {matching.RECURRING_DAYS_BEFORE.days}")
        self.assertContains(response, "pas cochée d&#x27;avance")
        # The group's own heading states the exception, with the figure and
        # the kinds of payment it holds for, worded from the matching's
        # constants: a heading reading « cochées d'avance » over an unticked
        # row owes the reader the rule. Failing first: « celles dont la ligne
        # dit pourquoi », a rule the reader had to find on each row.
        self.assertContains(
            response,
            f"cochées d&#x27;avance, sauf celles déliées à la main, celles dont la facture est datée au-delà de "
            f"{matching.RECURRING_DAYS_BEFORE.days} jours avant {escape(matching.NOT_BY_CARD)}",
        )
        # The subscription alone is ticked.
        self.assertContains(response, "1 sélectionnée")
        # The bank page says the distance under its pill too.
        self.assertContains(self.client.get(reverse("bank:bank_home")), f"datée {far} jours avant le paiement")
        # A reader who looked and ticked it: linked, as any accepted proposal.
        accepted = self.accept(ligne=[alarm.pk], **{f"option-{alarm.pk}": str(old.pk)})
        self.assertIn("1 proposition rattachée", self.messages_of(accepted))
        self.assertEqual(InvoicePayment.objects.get(transaction=alarm).invoice, old)

    def test_one_invoice_proposed_to_two_lines_is_ticked_for_the_first_only_and_said_on_both(self):
        """Two debits of the subscription at 23,70 €, 4 and 28 days after
        July's invoice, with June's and May's unpaid too: on its own each is
        NEAR_SURE on July's. Pre-ticked on both, the page promised two links
        it could make only one of, and a reader ticking the later one alone
        linked the wrong line. The first in the pass's order - the one
        `accept_proposals` would link - keeps its tick; the other says which
        operation takes the invoice first, and every option carrying it says
        who else wants it. Failing first: both ticked on July's, nothing
        said."""
        self.load(debit_row(date(2026, 7, 30), SUBSCRIPTION_PAYEE, "23,70"))
        later = BankTransaction.objects.get(counterparty=SUBSCRIPTION_PAYEE, operation_date=date(2026, 7, 30))
        response = self.client.get(self.url)
        ticked = checkboxes(response)
        self.assertEqual((ticked[self.subscription.pk], ticked[later.pk]), (True, False))
        self.assertFalse(any("checked" in tag for tag in radios(response, later)))
        self.assertContains(response, "proposée aussi pour l'opération du 30/07/2026")
        self.assertContains(response, "proposée aussi pour l'opération du 06/07/2026")
        self.assertContains(response, "l'opération du 06/07/2026")
        self.assertContains(response, "qui la prendrait la première")
        # The group's heading names this exception too.
        self.assertContains(response, "sauf celles déliées à la main et celles dont la facture est aussi proposée à une opération servie avant elles")
        self.assertContains(response, "1 sélectionnée")
        # POST what the page pre-ticked: one link, nothing skipped, and the
        # later debit comes back a question - its nearest invoice of that
        # amount is now June's, 58 days back.
        accepted = self.accept(ligne=[self.subscription.pk], **{f"option-{self.subscription.pk}": str(self.july.pk)})
        words = self.messages_of(accepted)
        self.assertIn("1 proposition rattachée", words)
        self.assertNotIn("entre-temps", words)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.july).transaction, self.subscription)
        self.assertEqual(self.group_order(accepted, later), 2)
        self.assertNotContains(accepted, "proposée aussi pour")


class AcceptProposalsViewTests(ProposalsPage, TestCase):
    def test_mixed_tiers_are_linked_each_to_its_option(self):
        response = self.accept(
            ligne=[self.subscription.pk, self.card.pk],
            **{f"option-{self.subscription.pk}": str(self.july.pk), f"option-{self.card.pk}": str(self.receipt.pk)},
        )
        self.assertIn("2 propositions rattachées", self.messages_of(response))
        for line, invoice in ((self.subscription, self.july), (self.card, self.receipt)):
            payment = InvoicePayment.objects.get(transaction=line)
            line.refresh_from_db()
            self.assertEqual((payment.invoice, payment.method, line.settled_by_hand), (invoice, InvoicePayment.Method.MANUAL, True))
        # The unticked line is untouched, and the payee the bank does not
        # spell is learnt from the link that added up.
        self.assertFalse(InvoicePayment.objects.filter(transaction=self.metro).exists())
        self.assertTrue(CounterpartyAlias.objects.filter(supplier=self.receipt.supplier, name="PAYTERM EPICERIE 12").exists())
        # Back on the page, with the two gone.
        self.assertEqual(set(checkboxes(response)), {self.metro.pk})

    def test_a_chosen_radio_beats_the_pre_ticked_one(self):
        self.accept(ligne=[self.subscription.pk], **{f"option-{self.subscription.pk}": str(self.june.pk)})
        self.assertEqual(InvoicePayment.objects.get(transaction=self.subscription).invoice, self.june)

    def test_a_sum_of_invoices_posts_every_id(self):
        """Two invoices of one supplier adding up to a debit travel as one
        option key with both ids; each id is checked, and the pair is what
        the matching must offer."""
        pair = debit_row(date(2026, 7, 9), "METRO FRANCE", "252,00")
        self.load(pair)
        both = BankTransaction.objects.get(counterparty="METRO FRANCE", amount=Decimal("-252.00"))
        response = self.client.get(self.url)
        self.assertContains(response, f'value="{self.first.pk} {self.second.pk}"')
        self.accept(ligne=[both.pk], **{f"option-{both.pk}": f"{self.first.pk} {self.second.pk}"})
        self.assertEqual(set(both.payments.values_list("invoice_id", flat=True)), {self.first.pk, self.second.pk})

    def test_an_income_line_never_settles_an_invoice(self):
        response = self.accept(ligne=[self.income.pk], **{f"option-{self.income.pk}": str(self.receipt.pk)})
        self.assertIn("une entrée d'argent ne règle pas une facture", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.exists())

    def test_an_invoice_paid_meanwhile_is_skipped_and_said(self):
        reconcile.link(self.rent, [self.receipt])
        response = self.accept(ligne=[self.card.pk], **{f"option-{self.card.pk}": str(self.receipt.pk)})
        self.assertIn("réglée entre-temps", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.filter(transaction=self.card).exists())

    def test_an_option_the_page_never_offered_is_skipped_and_said(self):
        response = self.accept(ligne=[self.subscription.pk], **{f"option-{self.subscription.pk}": str(self.first.pk)})
        self.assertIn("la proposition a changé", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.exists())

    def test_a_line_settled_meanwhile_is_skipped_and_said(self):
        reconcile.mark_no_invoice(self.card)
        response = self.accept(ligne=[self.card.pk], **{f"option-{self.card.pk}": str(self.receipt.pk)})
        self.assertIn("déjà rattachée, ou marquée", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.exists())

    def test_two_lines_wanting_one_invoice_link_it_once(self):
        self.load(debit_row(date(2026, 7, 3), "METRO FRANCE", "126,00"))
        later = BankTransaction.objects.get(counterparty="METRO FRANCE", operation_date=date(2026, 7, 3))
        response = self.accept(
            ligne=[later.pk, self.metro.pk],
            **{f"option-{later.pk}": str(self.first.pk), f"option-{self.metro.pk}": str(self.first.pk)},
        )
        words = self.messages_of(response)
        self.assertIn("1 proposition rattachée", words)
        self.assertIn("réglée entre-temps", words)
        self.assertEqual(InvoicePayment.objects.get(invoice=self.first).transaction, self.metro)

    def test_nothing_ticked_is_said(self):
        response = self.accept()
        self.assertIn("Cochez au moins une proposition", self.messages_of(response))

    def test_a_ticked_line_with_no_option_chosen_is_said(self):
        response = self.accept(ligne=[self.metro.pk])
        self.assertIn("sans facture choisie", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.exists())

    def test_a_line_that_is_gone_is_said(self):
        """A readable id of a line that no longer exists - deleted between the
        page being drawn and the POST: skipped, and said by its number since
        there is no line left to describe."""
        response = self.accept(ligne=["999999"], **{"option-999999": str(self.receipt.pk)})
        words = self.messages_of(response)
        self.assertIn("l'opération n'existe plus", words)
        self.assertIn("opération n° 999999", words)
        self.assertFalse(InvoicePayment.objects.exists())

    def test_ids_that_are_not_ids_never_reach_a_query(self):
        response = self.accept(
            ligne=["abc", "²", "1" * 19, str(self.card.pk)],
            **{"option-abc": "1", f"option-{self.card.pk}": "12 x"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("illisible", self.messages_of(response))
        self.assertFalse(InvoicePayment.objects.exists())

    def test_the_bulk_action_answers_a_get_with_a_redirect(self):
        response = self.client.get(self.post_url)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

    def test_the_per_row_forms_still_work_beside_it(self):
        self.client.post(reverse("bank:bank_line_action", args=[self.card.pk]), {"action": "link", "invoice": [self.receipt.pk]})
        self.assertEqual(InvoicePayment.objects.get(transaction=self.card).invoice, self.receipt)


class EmptyProposalsPageTests(Fixtures, TestCase):
    def test_before_any_statement(self):
        response = self.client.get(reverse("bank:proposals"))
        self.assertContains(response, "Aucun relevé importé")
        assertNoUnrenderedTemplateSyntax(self, response, "propositions vides")

    def test_lines_but_nothing_to_propose(self):
        self.load(debit_row(date(2026, 7, 20), "BAILLEUR EXEMPLE", "900,00"))
        response = self.client.get(reverse("bank:proposals"))
        self.assertContains(response, "Aucune proposition en attente")
        self.assertContains(response, "1 dépense reste sans facture")
