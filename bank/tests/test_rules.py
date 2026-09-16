"""Payments that never have an invoice, by a pattern on their label - and the
view of what is still missing one."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bank import reconcile
from bank.models import IgnoreRule, InvoicePayment
from bank.rules import compile_rules, ignoring_rule
from bank.tests.test_reconcile import Fixtures, debit_row
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

LOAN_ROW = "20/07/2026;ECHEANCE PRET;ECHEANCE PRET;ECHEANCE PRET 00000 00000000;19/07/2026;-1 500,00"


class Rule:
    def __init__(self, pattern):
        self.pattern = pattern


class RuleMatchingTests(SimpleTestCase):
    def test_a_rule_finds_its_words_anywhere_in_the_label_whatever_the_case(self):
        rules = compile_rules([Rule("urssaf")])
        self.assertIsNotNone(ignoring_rule("PRLV SEPA URSSAF D ILE DE FRANCE ECH/180826", rules))
        self.assertIsNone(ignoring_rule("PRLV SEPA METRO FRANCE ECH/090726", rules))

    def test_the_first_matching_rule_answers(self):
        loan, taxes = Rule("ECHEANCE PRET"), Rule("PRET|DGFIP")
        self.assertIs(ignoring_rule("ECHEANCE PRET 00000", compile_rules([loan, taxes])), loan)

    def test_a_broken_pattern_hides_nothing(self):
        self.assertEqual(compile_rules([Rule("URSSAF(")]), [])


class RuleValidationTests(TestCase):
    def test_a_pattern_that_does_not_compile_is_refused(self):
        with self.assertRaises(ValidationError):
            IgnoreRule(pattern="URSSAF(").full_clean()

    def test_a_pattern_that_matches_every_payment_is_refused(self):
        """One stray "|" would hide everything still missing its invoice."""
        for pattern in (".*", "URSSAF|", "^"):
            with self.subTest(pattern=pattern), self.assertRaises(ValidationError):
                IgnoreRule(pattern=pattern).full_clean()


class WithoutInvoiceTests(Fixtures, TestCase):
    def setUp(self):
        self.invoice("METRO", date(2026, 6, 29), "100.00")
        self.load(
            debit_row(date(2026, 7, 9), "METRO FRANCE", "120,00"),
            debit_row(date(2026, 7, 16), "URSSAF D ILE DE FRANCE", "700,00"),
            debit_row(date(2026, 8, 18), "URSSAF D ILE DE FRANCE", "750,00"),
            LOAN_ROW,
        )
        reconcile.reconcile()
        self.url = reverse("bank:bank_home")

    def stats(self, **params):
        return self.client.get(self.url, params).context["stats"]

    def test_the_payments_without_an_invoice_are_counted_and_added_up(self):
        stats = self.stats()
        self.assertEqual((stats["todo_count"], stats["todo_total"]), (3, Decimal("2950.00")))

    def test_by_payee(self):
        response = self.client.get(self.url, {"vue": "par-beneficiaire"})
        groups = {group.name: (group.count, group.total) for group in response.context["groups"]}
        self.assertEqual(
            groups, {"URSSAF D ILE DE FRANCE": (2, Decimal("1450.00")), "ECHEANCE PRET": (1, Decimal("1500.00"))}
        )
        self.assertContains(response, "?motif=URSSAF%20D%20ILE%20DE%20FRANCE")

    def test_one_month_at_a_time(self):
        stats = self.stats(mois="2026-08")
        self.assertEqual(
            (stats["todo_count"], stats["todo_total"], stats["spending_count"]), (1, Decimal("750.00"), 1)
        )
        self.assertEqual(self.stats(mois="n'importe")["spending_count"], 4)

    def test_a_rule_takes_its_payments_out_of_the_missing_ones(self):
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        stats = self.stats()
        self.assertEqual((stats["todo_count"], stats["todo_total"]), (1, Decimal("1500.00")))
        self.assertEqual((stats["no_invoice_count"], stats["by_rule_count"]), (2, 2))
        self.assertContains(self.client.get(self.url, {"vue": "sans-facture"}), "Cotisations")

    def test_a_paused_rule_hides_nothing(self):
        IgnoreRule.objects.create(pattern="URSSAF", is_active=False)
        self.assertEqual(self.stats()["todo_count"], 3)

    def test_a_rule_never_hides_a_payment_that_has_its_invoice(self):
        IgnoreRule.objects.create(pattern="METRO")
        self.assertEqual(self.stats()["linked_count"], 1)

    def test_automatic_matching_leaves_an_ignored_payment_alone(self):
        IgnoreRule.objects.create(pattern="METRO")
        InvoicePayment.objects.all().delete()
        self.assertEqual(reconcile.reconcile(), 0)

    def test_every_view_renders_with_a_rule_and_a_month(self):
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        for view in ("a-traiter", "par-beneficiaire", "rapprochees", "sans-facture", "toutes", "entrees"):
            with self.subTest(view=view):
                response = self.client.get(self.url, {"vue": view, "mois": "2026-07"})
                self.assertEqual(response.status_code, 200)
                assertNoUnrenderedTemplateSyntax(self, response, view)


class RulePageTests(Fixtures, TestCase):
    def setUp(self):
        self.load(
            debit_row(date(2026, 7, 16), "URSSAF D ILE DE FRANCE", "700,00"),
            debit_row(date(2026, 8, 18), "URSSAF D ILE DE FRANCE", "750,00"),
        )
        self.url = reverse("bank:rule_list")

    def test_the_page_is_prefilled_from_a_payee(self):
        response = self.client.get(self.url, {"motif": "URSSAF D ILE DE FRANCE", "nom": "URSSAF"})
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response)
        self.assertContains(response, 'value="URSSAF D ILE DE FRANCE"')

    def test_testing_a_pattern_shows_what_it_catches_without_saving_it(self):
        response = self.client.post(self.url, {"pattern": "urssaf", "description": "", "action": "test"})
        found = response.context["test"]
        self.assertEqual((found.count, found.total), (2, Decimal("1450.00")))
        self.assertFalse(IgnoreRule.objects.exists())

    def test_adding_a_rule(self):
        response = self.client.post(
            self.url, {"pattern": "URSSAF", "description": "Cotisations", "action": "add"}, follow=True
        )
        self.assertContains(response, "2 dépense(s)")
        self.assertEqual(IgnoreRule.objects.get().pattern, "URSSAF")

    def test_a_broken_pattern_is_explained_and_not_saved(self):
        response = self.client.post(self.url, {"pattern": "URSSAF(", "description": "", "action": "add"})
        self.assertContains(response, "invalide")
        self.assertFalse(IgnoreRule.objects.exists())

    def test_each_rule_says_what_it_catches(self):
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        ((_rule, found),) = self.client.get(self.url).context["rules"]
        self.assertEqual((found.count, found.total), (2, Decimal("1450.00")))

    def test_a_rule_can_be_paused_then_deleted(self):
        rule = IgnoreRule.objects.create(pattern="URSSAF")
        action = reverse("bank:rule_action", args=[rule.pk])
        self.client.post(action, {"action": "toggle"})
        rule.refresh_from_db()
        self.assertFalse(rule.is_active)
        self.client.post(action, {"action": "delete"})
        self.assertFalse(IgnoreRule.objects.exists())

    def test_rule_actions_only_answer_a_post(self):
        rule = IgnoreRule.objects.create(pattern="URSSAF")
        response = self.client.get(reverse("bank:rule_action", args=[rule.pk]), {"action": "delete"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(IgnoreRule.objects.exists())

    def test_an_empty_rules_page_offers_the_next_step(self):
        IgnoreRule.objects.all().delete()
        self.assertContains(self.client.get(self.url), "empty-state")
