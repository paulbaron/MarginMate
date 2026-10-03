"""Banque groups every amount's thousands (the owner, 01/10/2026: « des
espaces tous les 3 chiffres »): 25 000.00 €, never 25000.00 €, on the
operations, « Propositions », the rules, « Dépenses », « Entrées
d'argent » and « Trésorerie » alike, in the sentences the views write and in the pie's
tooltip - with a no-break space, so a figure never wraps across two lines.

What a script or a form reads beside it stays the figure as stored: a
cell's `data-sort`, an option's or a box's value.

Every payee, number and amount below is invented.
"""

from datetime import date

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from bank import reconcile
from bank.models import BankTransaction, TreasuryCheckpoint
from bank.tests.test_invoice_files import credit_row
from bank.tests.test_reconcile import Fixtures, debit_row

#: What separates an amount's thousands on a page (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"
JULY = {"du": "2026-07-01", "au": "2026-07-31"}


class GroupedFixtures(Fixtures):
    """A 25 000,00 € debit to METRO whose nearest invoice is 22 800,00 €
    TTC (19 000,00 € HT at 20 %): named, 2 200,00 € off - a question - and
    a second METRO debit of 1 500,00 €, a smaller unpaid invoice and a
    credit of 3 500,00 €."""

    def setUp(self):
        self.big = self.invoice("METRO", date(2026, 6, 29), "19000.00", invoice_number="M-9001")
        self.small = self.invoice("METRO", date(2026, 7, 1), "1000.00", invoice_number="M-9002")
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "25 000,00"),
            debit_row(date(2026, 7, 20), "METRO FRANCE", "1 500,00"),
            credit_row(date(2026, 7, 15), "CLIENT EXEMPLE", "3 500,00"),
        )
        self.debit = BankTransaction.objects.get(amount=-25000)
        self.second = BankTransaction.objects.get(amount=-1500)
        self.credit = BankTransaction.objects.get(amount=3500)

    def page(self, name="bank:bank_home", **parameters) -> str:
        response = self.client.get(reverse(name), parameters)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def said(self, response) -> list[str]:
        return [str(message) for message in get_messages(response.wsgi_request)]


class OperationsTests(GroupedFixtures, TestCase):
    def test_a_row_groups_its_amount_and_sorts_by_the_bare_figure(self):
        html = self.page(vue="toutes", **JULY)
        self.assertIn(f'data-sort="-25000.00">-25{NBSP}000.00 €</td>', html)
        self.assertIn(f'data-sort="-1500.00">-1{NBSP}500.00 €</td>', html)
        self.assertNotIn("25000.00 €", html)

    def test_the_stats_group_their_totals(self):
        html = self.page(**JULY)
        self.assertIn(f'<span class="stat-value">26{NBSP}500.00 €</span>', html)
        self.assertIn(f'<span class="stat-value">3{NBSP}500.00 €</span>', html)

    def test_the_suggestion_and_the_pick_list_group_theirs_and_post_ids(self):
        html = self.page(**JULY)
        self.assertIn(f"· 22{NBSP}800.00 €", html)
        self.assertIn(f"écart 2{NBSP}200.00 €", html)
        self.assertIn(f"écart le plus faible est de 2{NBSP}200.00 €", html)
        self.assertIn(f'<input type="hidden" name="invoice" value="{self.big.pk}">', html)
        self.assertIn(f'<option value="{self.big.pk}">', html)
        self.assertIn(f"29/06/2026 · 22{NBSP}800.00 €</option>", html)
        self.assertIn(f"01/07/2026 · 1{NBSP}200.00 €</option>", html)

    def test_a_linked_row_says_its_gap_and_who_else_pays_grouped(self):
        reconcile.link(self.debit, [self.big])
        reconcile.link(self.second, [self.big])
        html = self.page(vue="rapprochees", **JULY)
        self.assertIn(f"· 29/06/2026 · 22{NBSP}800.00 €", html)
        self.assertIn(
            f"Factures 22{NBSP}800.00 € pour 25{NBSP}000.00 € débités — 2{NBSP}200.00 € de moins que la dépense.",
            html,
        )
        self.assertIn(f"Aussi réglée par l'opération du 09/07/2026 (METRO FRANCE, 25{NBSP}000.00 €)", html)
        self.assertIn(f"Aussi réglée par l'opération du 20/07/2026 (METRO FRANCE, 1{NBSP}500.00 €)", html)

    def test_by_payee_groups_the_total(self):
        html = self.page(vue="par-beneficiaire", **JULY)
        self.assertIn(f'data-sort="26500.00">26{NBSP}500.00 €</td>', html)

    def test_the_search_finds_with_grouped_amounts_and_ticks_ids(self):
        reconcile.link(self.second, [self.big])
        response = self.client.get(reverse("bank:invoice_search", args=[self.debit.pk]), {"recherche": "M-9001"})
        html = response.content.decode()
        self.assertIn(f"· 29/06/2026 · 22{NBSP}800.00 €", html)
        self.assertIn(f"(METRO FRANCE, 1{NBSP}500.00 €)", html)
        self.assertIn(f'<input type="checkbox" name="invoice" value="{self.big.pk}">', html)


class ProposalsTests(GroupedFixtures, TestCase):
    def test_the_proposal_groups_its_amounts(self):
        html = self.page("bank:proposals")
        self.assertIn(f'aria-label="Rattacher l\'opération du 09/07/2026, 25{NBSP}000.00 €"', html)
        self.assertIn(f'data-sort="-25000.00">-25{NBSP}000.00 €</td>', html)
        self.assertIn(f"· 22{NBSP}800.00 €", html)
        self.assertIn(f"écart 2{NBSP}200.00 €", html)
        self.assertIn(f'name="option-{self.debit.pk}" value="{self.big.pk}"', html)

    def test_a_line_left_unlinked_is_named_with_its_amount_grouped(self):
        """An option the matching no longer offers is skipped, and the
        warning names the line as the page shows it."""
        response = self.client.post(
            reverse("bank:link_proposals"),
            {"ligne": [str(self.debit.pk)], f"option-{self.debit.pk}": str(self.small.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"09/07/2026 METRO FRANCE 25{NBSP}000.00 €", " ".join(self.said(response)))


class RulesTests(GroupedFixtures, TestCase):
    def test_testing_a_pattern_groups_what_it_catches(self):
        response = self.client.post(reverse("bank:rule_list"), {"pattern": "METRO", "action": "test"})
        html = response.content.decode()
        self.assertIn(f"<strong>2 dépenses</strong>, 26{NBSP}500.00 €,", html)
        self.assertIn(f'data-sort="-25000.00">-25{NBSP}000.00 €</td>', html)

    def test_adding_a_rule_says_what_it_takes_grouped_and_lists_it_so(self):
        response = self.client.post(reverse("bank:rule_list"), {"pattern": "METRO", "action": "add"})
        self.assertIn(
            f"Règle ajoutée : 2 dépense(s), 26{NBSP}500.00 €, ne comptent plus comme sans facture.",
            self.said(response),
        )
        self.assertRegex(self.page("bank:rule_list"), rf'data-sort="26500\.00">\s*26{NBSP}500\.00 €')


class SpendingTests(GroupedFixtures, TestCase):
    def setUp(self):
        super().setUp()
        BankTransaction.objects.filter(pk=self.debit.pk).update(category="Travaux inventés")

    def test_the_table_the_stats_and_the_list_group_their_amounts(self):
        html = self.page("bank:spending_home", **JULY)
        self.assertIn(f'<span class="stat-value">26{NBSP}500.00 €</span>', html)
        self.assertIn(f'data-sort="25000.00">25{NBSP}000.00 €</td>', html)
        self.assertIn(f'<th class="num">26{NBSP}500.00 €</th>', html)
        # The list of debits with no invoice: the bare figure sorts.
        self.assertIn(f'data-sort="-25000.00">25{NBSP}000.00 €</td>', html)
        # What the category box holds is the name, not a figure.
        self.assertIn('name="categorie" value="Travaux inventés"', html)

    def test_the_pie_s_tooltip_groups_the_amount(self):
        html = self.page("bank:spending_home", **JULY)
        self.assertIn(f'data-label="Travaux inventés" data-value="25{NBSP}000.00 € · ', html)

    def test_the_line_above_the_pie_groups_what_it_leaves_out(self):
        html = self.page("bank:spending_home", sans="Travaux inventés", **JULY)
        self.assertIn(f"Travaux inventés 25{NBSP}000.00 €", html)
        self.assertIn(f"25{NBSP}000.00 € hors du camembert sur", html)


class IncomeTests(GroupedFixtures, TestCase):
    def test_the_credit_and_its_totals_group_their_thousands(self):
        html = self.page("bank:income_home", **JULY)
        self.assertIn(f'<span class="stat-value">3{NBSP}500.00 €</span>', html)
        self.assertIn(f'data-sort="3500.00">3{NBSP}500.00 €</td>', html)
        self.assertIn(f"3{NBSP}500.00 € net", html)
        self.assertNotIn("3500.00 €", html)


class TreasuryTests(GroupedFixtures, TestCase):
    """« Trésorerie »: 30 000,00 € typed on 01/07, 6 000,00 € on 20/07 -
    1 000,00 € under what July's operations (-23 000,00 €) explain."""

    def setUp(self):
        super().setUp()
        TreasuryCheckpoint.objects.create(date=date(2026, 7, 1), balance=30000)
        TreasuryCheckpoint.objects.create(date=date(2026, 7, 20), balance=6000)

    def test_the_headline_the_tables_and_the_card_group_their_amounts(self):
        html = self.page("bank:treasury", tout="1")
        self.assertIn(f'<span class="stat-value">6{NBSP}000.00 €</span>', html)
        # The balances typed and the months: grouped, sorting by the bare figure.
        self.assertIn(f'data-sort="30000.00">30{NBSP}000.00 €</td>', html)
        self.assertIn(f'data-sort="-26500.00">-26{NBSP}500.00 €</td>', html)
        self.assertIn(f'data-sort="3500.00">3{NBSP}500.00 €</td>', html)
        self.assertIn(
            f"Les opérations expliquent -23{NBSP}000.00 €, l&#x27;écart est de 1{NBSP}000.00 € de moins.", html
        )
        self.assertIn(f"Ajouter un ajustement de -1{NBSP}000.00 €", html)
        self.assertIn(f"1{NBSP}000.00 € de moins : à résoudre", html)
        # What the form posts back is the bare figure.
        self.assertIn('name="ecart" value="-1000.00"', html)
        self.assertNotIn("30000.00 €", html)

    def test_the_messages_and_the_refusals_group_theirs(self):
        before, after = TreasuryCheckpoint.objects.order_by("date").values_list("pk", flat=True)
        response = self.client.post(
            reverse("bank:treasury_adjustment_add"), {"avant": before, "apres": after, "ecart": "-1000.00"}
        )
        self.assertEqual(
            self.said(response),
            [f"Ajustement de -1{NBSP}000.00 € ajouté au 20/07/2026 : les points du 01/07 et du 20/07 concordent."],
        )
        refused = self.client.post(reverse("bank:treasury"), {"date": "2026-07-01", "solde": "12 345,60"})
        self.assertContains(refused, f"Le 01/07/2026 a déjà un solde : 30{NBSP}000.00 €.")
