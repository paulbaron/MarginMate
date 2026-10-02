"""Payments that never have an invoice, by a pattern on their label - and the
view of what is still missing one."""

import re
from datetime import date
from decimal import Decimal
from unittest import mock

import regex
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bank import reconcile
from bank.forms import NUL_REFUSED, PATTERN_REQUIRED
from bank.models import IgnoreRule, InvoicePayment
from bank.rules import IgnoreRules, Matcher, compile_rules, ignoring_rule, searcher
from bank.tests.test_reconcile import Fixtures, debit_row
from bank.views import PayeeGroup
from returnables import patterns
from returnables.tests.test_patterns import NeverCompile
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

LOAN_ROW = "20/07/2026;ECHEANCE PRET;ECHEANCE PRET;ECHEANCE PRET 00000 00000000;19/07/2026;-1 500,00"

#: A count past what `re` can hold: `re.compile` raises OverflowError on
#: it - not re.error - and the form, and « Données »'s preview, were a 500.
HUGE_COUNT = "A{4294967296}"
#: A pattern the guard lets through - no count, nothing nested past its
#: bounds - that backtracks without end on a run of « A »: two ways to read
#: each one and no « B » to stop at, about 2**n steps, seconds at 22 for
#: the standard library and `regex` alike. Only the time limit stops it.
SLOW_PATTERN = r"(?:A |A  ?)+B"
SLOW_PAYEE = "A " * 22
#: Patterns `re` compiles and the guard refuses: a rule saved before it,
#: typed or imported, with what the refusal says.
REFUSED_NOW = (
    ("URSSAF D ILE|PAS{", "accolade"),
    ("URSSAF D ILE|A{101}", "répétition trop grande : 100 fois au plus"),
    ("(?x)URSSAF", "le mode (?x) n'est pas accepté"),
    ("URSSAF|[A[]", "crochet [ dans un ensemble"),
)
#: Patterns the guard refuses before anything compiles them: compiling
#: `(?:x{65535}){65535}` with `regex` froze the owner's PC.
NEVER_COMPILED = (
    (r"(?:x{65535}){65535}", "répétition trop grande"),
    (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté"),
    (r"a{e<=1}", "accolade"),
)


class Rule:
    def __init__(self, pattern, description=""):
        self.pattern = pattern
        self.description = description

    def __str__(self):
        return self.description or self.pattern


class RuleMatchingTests(SimpleTestCase):
    def test_a_rule_finds_its_words_anywhere_in_the_label_whatever_the_case(self):
        rules = compile_rules([Rule("urssaf")])
        self.assertIsNotNone(ignoring_rule("PRLV SEPA URSSAF D ILE DE FRANCE ECH/180826", rules))
        self.assertIsNone(ignoring_rule("PRLV SEPA METRO FRANCE ECH/090726", rules))

    def test_the_first_matching_rule_answers(self):
        loan, taxes = Rule("ECHEANCE PRET"), Rule("PRET|DGFIP")
        self.assertIs(ignoring_rule("ECHEANCE PRET 00000", compile_rules([loan, taxes])), loan)

    def test_a_broken_pattern_hides_nothing_and_says_why(self):
        rules = compile_rules([Rule("URSSAF(", "Cotisations")])
        self.assertEqual(rules.rules, ())
        self.assertIsNone(ignoring_rule("PRLV SEPA URSSAF(", rules))
        self.assertEqual(
            rules.problems,
            ["Règle « Cotisations » : parenthèse non fermée (position 7) - elle ne s'applique à aucune opération."],
        )

    def test_a_stored_pattern_the_guard_now_refuses_hides_nothing_and_says_why(self):
        """Saved before the guard - typed, or imported from another bar - a
        pattern `re` compiled and the guard refuses is set aside as a
        recognition rule is (`Rules.invalid`): its payments count as missing
        their invoice again, and the pages say which rule and why. The rules
        after it still answer."""
        label = "PRLV SEPA URSSAF D ILE DE FRANCE ECH/180826"
        loan = Rule("URSSAF", "Après")
        for pattern, reason in REFUSED_NOW:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(re.compile(pattern, re.IGNORECASE).search(label))
                stored = Rule(pattern, "Ancienne")
                rules = compile_rules([stored, loan])
                self.assertIs(ignoring_rule(label, rules), loan)
                ((refused, sentence),) = rules.invalid
                self.assertIs(refused, stored)
                self.assertIn(reason, sentence)
                (said,) = rules.problems
                self.assertTrue(said.startswith(f"Règle « Ancienne » : {reason}"), said)
                self.assertTrue(said.endswith(" - elle ne s'applique à aucune opération."), said)

    def test_the_patterns_the_pages_write_still_pass_and_find_what_re_found(self):
        """What « Ignorer… » pre-fills (`PayeeGroup.pattern`, the payee as
        printed, escaped) and the shapes the form's own help gives: every
        one passes the guard and finds exactly the labels `re` found."""
        payees = (
            "URSSAF D ILE DE FRANCE",
            "U.B.A.",
            "S.A.S. EXEMPLE (PARIS) & FILS",
            "SNCF*TGV 0123",
            "PRET N° 0000-1234/A",
            "DGFIP 75 IMPOT {TVA}",
            "CAFÉ ÉTOILE + CIE",
        )
        written = ["ECHEANCE PRET", "URSSAF", "DGFIP|MALAKOFF", ".*LOYER.*", r"ECH/\d{6}", r"\bSEPA\b"]
        labels = [f"PRLV SEPA {payee} ECH/180826" for payee in payees] + ["ECHEANCE PRET 00000", "LOYER JUILLET"]
        for pattern in [PayeeGroup(payee).pattern for payee in payees] + written:
            with self.subTest(pattern=pattern):
                rules = compile_rules([Rule(pattern)])
                self.assertEqual(rules.invalid, ())
                for label in labels:
                    self.assertEqual(
                        ignoring_rule(label, rules) is not None, bool(re.search(pattern, label, re.IGNORECASE))
                    )


class OutOfTime:
    """Stands in for a compiled pattern: its first `times` searches run out
    of time - TimeoutError, as `regex` says it - then it answers as `regex`
    would. Records what each search was asked, and how."""

    def __init__(self, pattern, times=1_000):
        self._regex = searcher(pattern)
        self.pattern = pattern
        self.left = times
        self.asked = []

    def search(self, text, **how):
        self.asked.append((text, how))
        if self.left > 0:
            self.left -= 1
            raise TimeoutError("regex time out")
        return self._regex.search(text)


def rules_of(*pairs) -> IgnoreRules:
    """The `IgnoreRules` of (rule, compiled pattern or stand-in) pairs."""
    return IgnoreRules(tuple((rule, Matcher(regex)) for rule, regex in pairs))


class SlowRuleTests(SimpleTestCase):
    """A pattern that passes the guard can still backtrack without end on
    some label: every match runs as a recognition rule's does - under the
    per-match limit, asked twice, billed its thread time - and a rule too
    slow is set aside for the rest of the reading - one page drawn, one
    automatic pass - and said."""

    def test_a_rule_out_of_time_twice_hides_nothing_from_then_on_and_the_next_rule_is_asked(self):
        slow, loan = Rule("URSSAF", "Cotisations"), Rule("PRET", "Prêt")
        frozen = OutOfTime("URSSAF")
        rules = rules_of((slow, frozen), (loan, searcher("PRET")))
        self.assertIsNone(ignoring_rule("PRLV SEPA URSSAF", rules))
        self.assertIs(ignoring_rule("URSSAF ECHEANCE PRET", rules), loan)
        # Asked twice - the limit is the clock's - then never again.
        self.assertEqual([text for text, _how in frozen.asked], ["PRLV SEPA URSSAF", "PRLV SEPA URSSAF"])
        self.assertEqual(rules.slow, [slow])
        self.assertEqual(rules.problems, ["Règle « Cotisations » : motif trop lent, ignoré - simplifiez-le."])
        # Another reading starts afresh.
        self.assertIs(ignoring_rule("PRLV SEPA URSSAF", compile_rules([slow])), slow)

    def test_a_match_out_of_time_once_is_asked_again_before_its_rule_is_set_aside(self):
        rule = Rule("URSSAF")
        once = OutOfTime("URSSAF", times=1)
        rules = rules_of((rule, once))
        self.assertIs(ignoring_rule("PRLV SEPA URSSAF", rules), rule)
        self.assertEqual((len(once.asked), rules.slow), (2, []))
        # Still applied to the next label.
        self.assertIs(ignoring_rule("URSSAF", rules), rule)

    def test_every_search_runs_under_the_per_match_limit_the_gil_released(self):
        """What `recognition.search` asks of `regex` for a recognition rule."""
        spy = OutOfTime("URSSAF", times=0)
        ignoring_rule("PRLV SEPA URSSAF", rules_of((Rule("URSSAF"), spy)))
        self.assertEqual(spy.asked, [("PRLV SEPA URSSAF", {"timeout": patterns.PATTERN_TIMEOUT, "concurrent": True})])

    def test_a_rule_slow_over_many_labels_is_set_aside_though_no_match_timed_out(self):
        rule = Rule("URSSAF")
        rules = compile_rules([rule])
        ticks = iter(range(0, 1000, 3))
        with mock.patch("bank.rules.thread_time", side_effect=lambda: float(next(ticks))):
            self.assertIs(ignoring_rule("URSSAF UN", rules), rule)
            self.assertEqual(rules.slow, [])
            # Three seconds a match: the second passes RULE_SECONDS.
            self.assertIs(ignoring_rule("URSSAF DEUX", rules), rule)
            self.assertEqual(rules.slow, [rule])
            self.assertIsNone(ignoring_rule("URSSAF TROIS", rules))

    def test_a_pattern_that_backtracks_without_end_is_stopped_by_the_time_limit(self):
        """For real, not simulated: `re` ran it to the end, seconds a label
        at 22 « A » and doubling with each one more, on every draw of Banque
        and « Dépenses »."""
        rules = compile_rules([Rule(SLOW_PATTERN, "Lente")])
        self.assertIsNone(ignoring_rule(f"PRLV SEPA {SLOW_PAYEE}ECH/200726", rules))
        self.assertEqual([str(rule) for rule in rules.slow], ["Lente"])


class RuleValidationTests(TestCase):
    def setUp(self):
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)

    def refusal(self, pattern) -> list[str]:
        with self.assertRaises(ValidationError) as refused:
            IgnoreRule(pattern=pattern).full_clean()
        return refused.exception.message_dict["pattern"]

    def test_a_pattern_that_does_not_compile_is_refused(self):
        self.assertEqual(self.refusal("URSSAF("), ["Motif : parenthèse non fermée (position 7)."])

    def test_a_pattern_that_matches_every_payment_is_refused(self):
        """One stray "|" would hide everything still missing its invoice."""
        for pattern in (".*", "URSSAF|", "^", ".*URSSAF|"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    self.refusal(pattern),
                    [
                        (
                            "Motif : le motif accepte une ligne vide : il trouverait quelque chose sur n'importe "
                            "quelle ligne."
                        )
                    ],
                )

    def test_a_count_past_what_re_can_hold_is_refused_not_a_crash(self):
        self.assertEqual(self.refusal(HUGE_COUNT), ["Motif : motif invalide."])

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        for pattern, reason in NEVER_COMPILED:
            with self.subTest(pattern=pattern):
                never = NeverCompile()
                with mock.patch.object(regex, "compile", new=never):
                    (said,) = self.refusal(pattern)
                self.assertEqual(never.calls, [])
                self.assertTrue(said.startswith("Motif : "), said)
                self.assertIn(reason, said)

    def test_a_pattern_re_compiled_and_the_guard_refuses_is_refused(self):
        for pattern, reason in REFUSED_NOW:
            with self.subTest(pattern=pattern):
                (said,) = self.refusal(pattern)
                self.assertIn(reason, said)


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
        self.assertEqual((stats["todo_count"], stats["todo_total"], stats["spending_count"]), (1, Decimal("750.00"), 1))
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

    def test_a_stored_rule_the_guard_refuses_hides_nothing_and_the_page_says_so(self):
        """Its payments are back among the missing ones - said, with the way
        to the rule, rather than the count moving with nothing to say why."""
        IgnoreRule.objects.create(pattern="URSSAF D ILE|PAS{", description="Cotisations")
        response = self.client.get(self.url)
        self.assertEqual(response.context["stats"]["todo_count"], 3)
        (said,) = response.context["ignore_problems"]
        self.assertTrue(said.startswith("Règle « Cotisations » : accolade : "), said)
        self.assertContains(response, "Règle « Cotisations » : accolade : ")
        self.assertContains(response, reverse("bank:rule_list"))
        assertNoUnrenderedTemplateSyntax(self, response)

    def test_a_rule_too_slow_on_a_label_is_set_aside_and_the_page_says_so(self):
        self.load(debit_row(date(2026, 7, 20), SLOW_PAYEE, "10,00"))
        IgnoreRule.objects.create(pattern=SLOW_PATTERN, description="Lente")
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["ignore_problems"], ["Règle « Lente » : motif trop lent, ignoré - simplifiez-le."]
        )
        self.assertContains(response, "Règle « Lente » : motif trop lent, ignoré - simplifiez-le.")
        # The other rule still hides its payments.
        stats = response.context["stats"]
        self.assertEqual((stats["todo_count"], stats["by_rule_count"]), (2, 2))

    def test_nothing_is_said_while_every_rule_applies(self):
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        response = self.client.get(self.url)
        self.assertEqual(response.context["ignore_problems"], [])
        self.assertNotContains(response, "Dépenses sans facture attendue »</a>")


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
        self.assertEqual(response.context["form"].errors["pattern"], ["Motif : parenthèse non fermée (position 7)."])
        self.assertFalse(IgnoreRule.objects.exists())

    def test_the_motif_is_refused_in_french_and_once(self):
        """The site speaks English to Django: its own « This field is
        required. » reached the page, beside the model's sentence about the
        same empty value."""
        for pattern, said in (
            ("", PATTERN_REQUIRED),
            ("URS\x00SAF", NUL_REFUSED),
            ("U" * 256, "255 caractères au plus (256 ici)."),
        ):
            with self.subTest(pattern=pattern[:10]):
                response = self.client.post(self.url, {"pattern": pattern, "action": "add"})
                self.assertEqual(response.context["form"].errors["pattern"], [said])
        self.assertFalse(IgnoreRule.objects.exists())

    def test_a_count_past_what_re_can_hold_is_said_on_the_motif_not_a_500(self):
        for action in ("test", "add"):
            with self.subTest(action=action):
                response = self.client.post(self.url, {"pattern": HUGE_COUNT, "description": "", "action": action})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"].errors["pattern"], ["Motif : motif invalide."])
                self.assertIsNone(response.context["test"])
        self.assertFalse(IgnoreRule.objects.exists())

    def test_a_pattern_the_guard_refuses_is_never_compiled(self):
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        for pattern, reason in NEVER_COMPILED:
            for action in ("test", "add"):
                with self.subTest(pattern=pattern, action=action):
                    never = NeverCompile()
                    with mock.patch.object(regex, "compile", new=never):
                        response = self.client.post(self.url, {"pattern": pattern, "action": action})
                    self.assertEqual(never.calls, [])
                    (said,) = response.context["form"].errors["pattern"]
                    self.assertIn(reason, said)
        self.assertFalse(IgnoreRule.objects.exists())

    def test_a_pattern_too_slow_on_the_imported_lines_is_refused_and_not_saved(self):
        """« Tester » and « Ajouter » run the pattern over every debit
        imported: one that stalls on a label is said on the motif, never
        saved - kept, it would be set aside on every page it is drawn on."""
        self.load(debit_row(date(2026, 7, 20), SLOW_PAYEE, "10,00"))
        for action in ("test", "add"):
            with self.subTest(action=action):
                response = self.client.post(
                    self.url, {"pattern": SLOW_PATTERN, "description": "Lente", "action": action}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["form"].errors["pattern"],
                    ["Motif : le motif est trop lent sur les opérations importées : simplifiez-le."],
                )
                self.assertIsNone(response.context["test"])
        self.assertFalse(IgnoreRule.objects.exists())

    def test_a_stored_rule_that_cannot_be_applied_is_marked_and_counts_nothing(self):
        IgnoreRule.objects.create(pattern="URSSAF D ILE|PAS{", description="Ancienne")
        self.load(debit_row(date(2026, 7, 20), SLOW_PAYEE, "10,00"))
        IgnoreRule.objects.create(pattern=SLOW_PATTERN, description="Lente")
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        response = self.client.get(self.url)
        found = {str(rule): found for rule, found in response.context["rules"]}
        self.assertEqual(found["Ancienne"].problem[:11], "accolade : ")
        self.assertEqual((found["Ancienne"].slow, found["Ancienne"].count), (False, None))
        self.assertEqual((found["Lente"].problem, found["Lente"].slow, found["Lente"].count), ("", True, None))
        self.assertEqual(
            (found["Cotisations"].count, found["Cotisations"].problem, found["Cotisations"].slow), (2, "", False)
        )
        self.assertContains(response, "motif invalide")
        self.assertContains(response, "trop lent")
        assertNoUnrenderedTemplateSyntax(self, response)

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


class RuleCategoryTests(Fixtures, TestCase):
    """A rule's category is editable from the rules page.

    « Dépenses » tells the reader that giving a category to a rule is THE way
    to stop typing the same word every month - and every rule written before
    that field exists carries none. With no control here, correcting a typo
    meant deleting the rule and retyping its pattern, with nothing stopping a
    duplicate.
    """

    def setUp(self):
        self.load(debit_row(date(2026, 7, 16), "URSSAF D ILE DE FRANCE", "700,00"))
        self.rule = IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        self.url = reverse("bank:rule_list")
        self.action = reverse("bank:rule_action", args=[self.rule.pk])

    def test_the_page_offers_a_category_for_every_rule(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="categorie"')
        self.assertContains(page, 'value="category"')
        assertNoUnrenderedTemplateSyntax(self, page)

    def test_a_category_given_to_a_rule_is_stored_and_said(self):
        answer = self.client.post(self.action, {"action": "category", "categorie": "Charges sociales"})
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.category, "Charges sociales")
        self.assertContains(self.client.get(answer.url), "Charges sociales")

    def test_a_category_is_cleaned_the_way_a_lines_own_is(self):
        """It reaches the same column and the same pie, so it takes the same
        guards: a control character is « A string literal cannot contain NUL »
        on the INSERT, and an over-wide string is Django's problem on every
        read afterwards."""
        self.client.post(self.action, {"action": "category", "categorie": "Char\x00ges   sociales "})
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.category, "Charges sociales")

        self.client.post(self.action, {"action": "category", "categorie": "X" * 400})
        self.rule.refresh_from_db()
        self.assertEqual(len(self.rule.category), 255)

    def test_a_category_taken_off_a_rule_says_what_its_spending_counts_as(self):
        self.rule.category = "Charges sociales"
        self.rule.save(update_fields=["category"])
        answer = self.client.post(self.action, {"action": "category", "categorie": ""})
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.category, "")
        self.assertContains(self.client.get(answer.url), "Sans catégorie")

    def test_the_pattern_is_never_touched_by_a_category(self):
        """Editing what a rule DECIDES ON in passing would silently change
        which lines it catches."""
        self.client.post(self.action, {"action": "category", "categorie": "Charges sociales", "pattern": "TOUT"})
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.pattern, "URSSAF")

    def test_a_rules_category_reaches_the_spending_page(self):
        self.client.post(self.action, {"action": "category", "categorie": "Charges sociales"})
        page = self.client.get(reverse("bank:spending_home"), {"du": "2026-07-01", "au": "2026-07-31"})
        self.assertContains(page, "Charges sociales")
        self.assertContains(page, "par règle")
