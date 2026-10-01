"""« Reconnaissance des opérations » (`/banque/reconnaissance/`, bank/views.py:
`recognition_page`, `recognition_rule`, `recognition_reapply`) as the owner
uses it - every form read off the page it drew and posted as a browser
posts it (staff/tests/page_forms.py), its CSRF token checked:

* the list shows the rules of both questions - what an operation is, what a
  credit is in the till - in their order, and how many stored lines each
  decides now; a stored rule the check refuses is said on its row;
* a new rule is checked before it is stored (its name whatever its case and
  accents, its pattern by `recognition.check`), each refusal said once, in
  French, and comes last of its question;
* « Tester » tries the rule as typed on the stored lines and saves nothing:
  how many it finds, how many a rule before it already decides, the newest
  of them with what it reads;
* a rule is edited, moved up or down among the rules of ITS question only,
  suspended, reactivated, deleted - and nothing but a POST writes;
* « Relire les opérations déjà importées » shows what would change, then
  writes exactly that, or nothing when the rules or the lines moved since;
* « Entrées d'argent » and Banque say where the rules are.

Every label, payee and amount below is invented.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html import unescape
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bank import income, recognition, views
from bank.forms import OperationRuleForm
from bank.models import BankTransaction, CounterpartyAlias, IncomePayer, OperationRule
from bank.tests.support import SEED, make_rule, rule
from bank.tests.test_recognition import too_slow
from invoices.models import Supplier
from staff.tests.page_forms import as_post, form_posting_to
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

CARD, DEBIT, TRANSFER, OTHER = (kind.value for kind in BankTransaction.Kind)
Meaning = OperationRule.Meaning

#: The seeded rules' names, as the page shows them.
CARD_RULE = "Paiement par carte (FACTURE CARTE)"
DEBIT_RULE = "Prélèvement (PRLV SEPA)"
SENT_RULE = "Virement émis (/BEN)"
RECEIVED_RULE = "Virement reçu (/FRM)"
ANY_TRANSFER_RULE = "Autre virement (VIR)"
PAYOUT_RULE = "Versement carte (TOTAL ENCAISSE)"
CASH_RULE = "Dépôt d'espèces (VERSEMENT ESPECES)"
CHEQUE_RULE = "Remise de chèques (REMISE CHEQUE)"
KIND_RULES = [CARD_RULE, DEBIT_RULE, SENT_RULE, RECEIVED_RULE, ANY_TRANSFER_RULE]
TILL_RULES = [PAYOUT_RULE, CASH_RULE, CHEQUE_RULE]

#: A terminal no seeded rule recognises: its payouts print no gross.
TERMINAL_LABEL = "REMISE TPE {number:06d} BAR EXEMPLE"
TPE_RULE = "Versement TPE (REMISE TPE)"


def text_of(html: str) -> str:
    """Markup as a reader reads it: tags out, entities decoded, whitespace folded."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def table_of(html: str, label: str) -> str:
    start = html.rindex("<table", 0, html.index(f'data-table-label="{label}"'))
    return html[start : html.index("</table>", start)]


def row_of(html: str, rule: OperationRule) -> str:
    start = html.index(f'<tr id="regle-{rule.pk}">')
    return html[start : html.index("</tr>", start)]


def seeded(name) -> OperationRule:
    return OperationRule.objects.get(name=name)


def another_tab_takes_the_name():
    """The name the form has just checked, saved by another request before
    this one writes - the race no check made before the write can see (two
    tabs, a double click)."""
    real = OperationRuleForm.clean_name

    def racing(form):
        name = real(form)
        make_rule(name, Meaning.CHEQUE, "AUTRE ONGLET", "bank_type")
        return name

    return mock.patch.object(OperationRuleForm, "clean_name", racing)


class Page(TestCase):
    """The owner's bank's statement, invented: a card payment, a direct
    debit, a card payout printing its gross, a cash deposit - each stored as
    the seeded rules read it at import."""

    def setUp(self):
        super().setUp()
        # The suite's client (tests/runner.py), logged in, CSRF enforced as
        # a browser's is.
        self.client = self.client_class(enforce_csrf_checks=True)
        self.url = reverse("bank:recognition")
        self.counter = 0
        self.card = self.line(
            date(2026, 7, 15),
            "FACTURE CARTE DU 140726 EPICERIE EXEMPLE CARTE 4974XXXXXXXX1111",
            "-12.30",
            "FACTURE CARTE",
            kind=CARD,
            counterparty="EPICERIE EXEMPLE",
            card_date=date(2026, 7, 14),
        )
        self.debit = self.line(
            date(2026, 7, 9),
            "PRLV SEPA FOURNISSEUR EXEMPLE ECH/090726 ID EMETTEUR/FR00ZZZ000000 REF/0000",
            "-120.35",
            "PRLV SEPA",
            kind=DEBIT,
            counterparty="FOURNISSEUR EXEMPLE",
        )
        self.payout = self.line(
            date(2026, 7, 3),
            "VIR SEPA RECU /FRM BAR EXEMPLE /EID /RNF TRANSFERT 0000001 TOTAL ENCAISSE 200.00 EUROS BAR EXEMPLE",
            "198.60",
            "VIREMENT",
            kind=TRANSFER,
            counterparty="BAR EXEMPLE",
        )
        self.cash = self.line(date(2026, 7, 10), "VERSEMENT ESPECES 0001", "40.00", "VERSEMENT ESPECES")

    def line(self, day, label, amount, bank_type="", *, kind=OTHER, counterparty="", card_date=None):
        self.counter += 1
        return BankTransaction.objects.create(
            operation_date=day,
            label=label,
            amount=Decimal(amount),
            bank_type=bank_type,
            kind=kind,
            counterparty=counterparty,
            card_date=card_date,
            fingerprint=f"reconnaissance-{self.counter}",
        )

    def terminal(self, day, amount, number=None):
        """A payout of the terminal no seeded rule recognises."""
        self.counter += 1
        return self.line(day, TERMINAL_LABEL.format(number=number or self.counter), amount, "VIREMENT")

    def get(self, url=None, status=200):
        url = url or self.url
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        if status == 200:
            assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def html(self, url=None) -> str:
        return self.get(url).content.decode()

    def send(self, form, *, press=None, values=None, follow=True):
        """`form` submitted as the browser would."""
        response = self.client.post(
            form.action.split("#")[0], as_post(form.submission(press=press, values=values)), follow=follow
        )
        if follow and response.status_code == 200:
            assertNoUnrenderedTemplateSyntax(self, response, form.action)
        return response

    def new_rule_form(self, html=None):
        return form_posting_to(html or self.html(), self.url)

    def add(self, *, press=("action", "enregistrer"), **values):
        """The « Nouvelle règle » card, filled in and pressed."""
        typed = {"name": TPE_RULE, "meaning": Meaning.PAYOUT, "searched": "label", "pattern": "REMISE TPE"}
        typed.update(values)
        return self.send(self.new_rule_form(), press=press, values=typed)

    def rule_url(self, rule) -> str:
        return reverse("bank:recognition_rule", args=[rule.pk])

    def button_form(self, rule, action, html=None):
        """The list's form posting `action` on `rule`."""
        return form_posting_to(html or self.html(), self.rule_url(rule), holding=("action", action))

    def press(self, rule, action):
        return self.send(self.button_form(rule, action), press=("action", action))

    def edit(self, rule, *, press=("action", "enregistrer"), **values):
        """The rule's own page, its form changed and pressed."""
        url = self.rule_url(rule)
        form = form_posting_to(self.html(url), url, holding=("action", "tester"))
        return self.send(form, press=press, values=values)

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def order(self, response, part="kind_rows") -> list[str]:
        return [row.rule.name for row in response.context[part]]

    def stored(self) -> list[list]:
        """Every rule as stored, and every line's three readings."""
        return [
            list(
                OperationRule.objects.order_by("pk").values_list("name", "meaning", "pattern", "position", "is_active")
            ),
            list(BankTransaction.objects.order_by("pk").values_list("kind", "counterparty", "card_date")),
        ]


class ListTests(Page):
    def test_both_questions_in_their_order_with_what_each_decides_now(self):
        response = self.get()
        self.assertEqual(self.order(response), KIND_RULES)
        self.assertEqual(self.order(response, "till_rows"), TILL_RULES)
        self.assertEqual(
            [(row.order, row.decided) for row in response.context["kind_rows"]],
            [(1, 1), (2, 1), (3, 0), (4, 1), (5, 0)],
        )
        self.assertEqual([(row.order, row.decided) for row in response.context["till_rows"]], [(1, 1), (2, 1), (3, 0)])
        html = response.content.decode()
        kinds = text_of(table_of(html, "règles de nature"))
        till = text_of(table_of(html, "règles en caisse"))
        self.assertEqual([kinds.index(name) for name in KIND_RULES], sorted(kinds.index(name) for name in KIND_RULES))
        self.assertEqual([till.index(name) for name in TILL_RULES], sorted(till.index(name) for name in TILL_RULES))
        self.assertLess(html.index('id="nature"'), html.index('id="en-caisse"'))
        # A row as it reads: its place, its meaning, where it looks, its
        # pattern, what it decides, its state.
        payout = text_of(row_of(html, seeded(PAYOUT_RULE)))
        self.assertTrue(payout.startswith("1 ↑ ↓ Versement carte (TOTAL ENCAISSE) Versement de carte (TPE) brut lu"))
        self.assertIn("Libellé TOTAL", payout)
        self.assertIn("EUROS?\\b 1 Active Modifier Suspendre Supprimer", payout)
        self.assertIn(
            "Dépôt d'espèces Type d'opération VERSEMENT ESPECES 1 Active", text_of(row_of(html, seeded(CASH_RULE)))
        )

    def test_a_payout_rule_reading_no_gross_says_the_amount_received_counts(self):
        make_rule(TPE_RULE, Meaning.PAYOUT, "REMISE TPE")
        self.terminal(date(2026, 7, 6), "150.00")
        html = self.html()
        self.assertIn(
            f"4 ↑ ↓ {TPE_RULE} Versement de carte (TPE) brut non imprimé : montant reçu Libellé REMISE TPE 1 Active",
            text_of(row_of(html, OperationRule.objects.get(name=TPE_RULE))),
        )

    def test_with_the_seeded_rules_nothing_is_to_read_again_nor_blocked(self):
        response = self.get()
        self.assertEqual(response.context["pending"], 0)
        self.assertFalse(response.context["blocked"])
        self.assertNotContains(response, reverse("bank:recognition_reapply"))
        self.assertNotContains(response, "motif invalide")
        self.assertContains(response, "Lue à l'import : une règle modifiée vaut pour les relevés importés ensuite.")

    def test_a_suspended_rule_decides_nothing_and_says_so(self):
        OperationRule.objects.filter(name=CASH_RULE).update(is_active=False)
        html = self.html()
        row = text_of(row_of(html, seeded(CASH_RULE)))
        self.assertIn("VERSEMENT ESPECES — Suspendue Modifier Réactiver Supprimer", row)

    def test_a_stored_rule_the_check_refuses_is_said_on_its_row_and_stops_the_imports(self):
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern="PRLV (")
        response = self.get()
        html = response.content.decode()
        self.assertIn(
            "PRLV ( motif invalide parenthèse non fermée (position 6). — Active",
            text_of(row_of(html, seeded(DEBIT_RULE))),
        )
        self.assertContains(
            response,
            "Aucun relevé ne s'importe tant qu'une règle active a un motif invalide ou trop lent : corrigez-la ou "
            "suspendez-la.",
        )
        # Its own page says it too, above the form that fixes it.
        page = text_of(self.html(self.rule_url(seeded(DEBIT_RULE))))
        self.assertIn("Motif invalide : parenthèse non fermée (position 6) - la règle ne reconnaît rien", page)

    def test_a_suspended_invalid_rule_is_said_and_stops_nothing(self):
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern="PRLV (", is_active=False)
        response = self.get()
        self.assertIn("motif invalide", text_of(row_of(response.content.decode(), seeded(DEBIT_RULE))))
        self.assertFalse(response.context["blocked"])
        self.assertNotContains(response, "Aucun relevé ne s'importe")

    def test_a_rule_found_too_slow_while_the_page_reads_is_said(self):
        with too_slow(SEED.RULES[1][4]):
            response = self.get()
        self.assertIn(
            "trop lent Ignoré : simplifiez-le.", text_of(row_of(response.content.decode(), seeded(DEBIT_RULE)))
        )
        self.assertTrue(response.context["blocked"])

    def test_a_till_rule_found_too_slow_is_said_and_stops_no_import(self):
        """An import reads what the operation is, never what a credit is in
        the till: a till rule found slow here is no reason to refuse one, nor
        « Relire »."""
        with too_slow(SEED.RULES[5][4]):
            response = self.get()
            reapply = self.get(reverse("bank:recognition_reapply"))
        self.assertIn(
            "trop lent Ignoré : simplifiez-le.", text_of(row_of(response.content.decode(), seeded(PAYOUT_RULE)))
        )
        self.assertFalse(response.context["blocked"])
        self.assertNotContains(response, "Aucun relevé ne s'importe")
        self.assertFalse(reapply.context["blocked"])

    def test_an_invalid_till_rule_stops_the_imports(self):
        # Every active rule is compiled at import: one that fails the check
        # refuses it, whichever question it answers.
        OperationRule.objects.filter(name=CHEQUE_RULE).update(pattern="REMISE (")
        response = self.get()
        self.assertTrue(response.context["blocked"])
        self.assertContains(response, "Aucun relevé ne s'importe")

    def test_a_credit_chosen_by_hand_is_no_line_the_till_rule_decides(self):
        """The line's own « En caisse » choice beats every rule
        (`income.reading_of`): the payout set to « Pas une vente » by hand is
        no longer one the payout rule decides."""
        BankTransaction.objects.filter(pk=self.payout.pk).update(income_source="other")
        response = self.get()
        self.assertEqual([row.decided for row in response.context["till_rows"]], [0, 1, 0])
        # What the operation is stays decided by its rule, whatever the till says.
        self.assertEqual(
            [row.decided for row in response.context["kind_rows"]],
            [1, 1, 0, 1, 0],
        )

    def test_no_rule_at_all(self):
        OperationRule.objects.all().delete()
        response = self.get()
        self.assertContains(response, "Aucune règle : rien n'est reconnu ici.", count=2)
        # What the lines would read with no rule: « Autre », no payee, no date.
        self.assertEqual(response.context["pending"], 3)
        self.assertEqual(len(recognition.stored_changes().changes), 3)

    def test_the_explainer_says_how_a_motif_is_written_with_examples_that_read_as_said(self):
        html = self.html()
        help_text = text_of(html[html.index('<details class="explainer pattern-help">') :])
        self.assertIn("La première règle active de sa partie, dans l'ordre, qui trouve son motif décide.", help_text)
        self.assertIn(
            "Majuscules et minuscules sont confondues ; les accents aussi, quand le motif n'en écrit pas.", help_text
        )
        self.assertIn(
            "(?P<encaisse>…) la somme encaissée (brut, avant commission) : Versement de carte (TPE).", help_text
        )
        self.assertIn(
            "(?P<tiers>…) le tiers (bénéficiaire ou payeur) : Paiement par carte, Prélèvement, Virement, Autre "
            "opération.",
            help_text,
        )
        for example in views.PATTERN_EXAMPLES:
            with self.subTest(example=example.label):
                self.assertIn(f"« {example.label} » : {example.pattern} → {example.reads}.", help_text)

    def test_banque_s_header_leads_here(self):
        html = self.html(reverse("bank:bank_home"))
        actions = html[html.index('<div class="actions">') :]
        actions = actions[: actions.index("</div>")]
        self.assertIn(f'<a class="btn btn-secondary" href="{self.url}">Reconnaissance des opérations</a>', actions)


class QueryTests(Page):
    def test_the_list_costs_the_same_with_more_lines_and_more_rules(self):
        """The rules are read once and the lines in one pass, whatever their
        number - with a rule being tried too."""

        def cost() -> tuple[int, int]:
            with CaptureQueriesContext(connection) as listed:
                self.get()
            with CaptureQueriesContext(connection) as tried:
                self.add(press=("action", "tester"))
            return len(listed.captured_queries), len(tried.captured_queries)

        few = cost()
        for number in range(30):
            self.terminal(date(2026, 6, 1 + number % 28), "10.00", number=100 + number)
        make_rule(TPE_RULE, Meaning.PAYOUT, "REMISE TPE")
        make_rule("Virement instantané (VIR INST)", Meaning.TRANSFER, "^VIR INST")
        self.assertEqual(cost(), few)


class PatternExampleTests(TestCase):
    """What « Écrire un motif » says each example reads, read."""

    def rules(self, example):
        return recognition.compile_rules([rule("exemple", example.meaning, example.pattern, example.searched)])

    def test_every_example_passes_the_check(self):
        for example in views.PATTERN_EXAMPLES:
            with self.subTest(example=example.label):
                recognition.check(example.meaning, example.searched, example.pattern)

    def test_the_card_payment_reads_its_payee_and_its_day(self):
        card = views.PATTERN_EXAMPLES[0]
        found = recognition.describe(self.rules(card), card.label, "", date(2026, 7, 16))
        self.assertEqual(
            (found.kind, found.counterparty, found.card_date), (CARD, "BOULANGERIE EXEMPLE", date(2026, 7, 14))
        )

    def test_a_payout_without_its_gross_and_one_with_it(self):
        without, with_gross = views.PATTERN_EXAMPLES[1:]
        self.assertEqual(recognition.till_reading(self.rules(without), without.label).gross, None)
        self.assertEqual(recognition.till_reading(self.rules(without), without.label).source, "card")
        self.assertEqual(recognition.till_reading(self.rules(with_gross), with_gross.label).gross, Decimal("1234.56"))


class NewRuleTests(Page):
    def test_a_new_rule_is_stored_last_of_its_question(self):
        before = OperationRule.objects.order_by("-position").first().position
        response = self.add()
        self.assertEqual(
            response.redirect_chain[-1][0], f"{self.url}#regle-{OperationRule.objects.get(name=TPE_RULE).pk}"
        )
        self.assertEqual(self.messages_of(response), [f"Règle « {TPE_RULE} » ajoutée."])
        made = OperationRule.objects.get(name=TPE_RULE)
        self.assertEqual(
            (made.meaning, made.searched, made.pattern, made.position, made.is_active),
            (Meaning.PAYOUT, "label", "REMISE TPE", before + 1, True),
        )
        self.assertEqual(self.order(response, "till_rows"), [*TILL_RULES, TPE_RULE])

    def test_a_rule_of_what_the_operation_is_says_it_reads_the_next_imports(self):
        response = self.add(name="Virement instantané (VIR INST)", meaning=Meaning.TRANSFER, pattern=r"^VIR INST")
        self.assertEqual(
            self.messages_of(response),
            ["Règle « Virement instantané (VIR INST) » ajoutée : elle vaut pour les relevés importés ensuite."],
        )
        self.assertEqual(self.order(response), [*KIND_RULES, "Virement instantané (VIR INST)"])

    def test_the_name_and_the_pattern_are_kept_as_typed_but_for_their_spaces(self):
        self.add(name="  Versement   TPE  ", pattern="  REMISE TPE  ")
        self.assertEqual(OperationRule.objects.filter(name="Versement TPE", pattern="REMISE TPE").count(), 1)

    def test_the_menu_groups_the_meanings_by_question_nothing_chosen(self):
        html = self.html()
        menu = html[html.index('<select name="meaning"') :]
        menu = menu[: menu.index("</select>")]
        self.assertIn('<option value="" selected>— choisissez —</option>', menu.split("<optgroup")[0])
        self.assertLess(
            menu.index('<optgroup label="Nature de l&#x27;opération (lue à l&#x27;import)">'),
            menu.index('<optgroup label="En caisse (entrées d&#x27;argent)">'),
        )
        kinds = menu[: menu.index('<optgroup label="En caisse')]
        till = menu[menu.index('<optgroup label="En caisse') :]
        for value in ("card_payment", "debit", "transfer", "other_operation"):
            self.assertIn(f'value="{value}"', kinds)
        for value in ("payout", "cash", "cheque", "voucher", "credit", "not_a_sale"):
            self.assertIn(f'value="{value}"', till)
        # The pattern is typed as code.
        start = html.rindex("<input", 0, html.index('name="pattern"'))
        pattern = html[start : html.index(">", start)]
        for attribute in ('class="pattern-input"', 'autocapitalize="off"', 'spellcheck="false"', 'autocomplete="off"'):
            self.assertIn(attribute, pattern)

    def assertRefused(self, response, sentence, errors=1):
        """Refused on the page drawn again - said once, and nothing else said
        about it: `errors` sentences under the form's fields in all - and
        nothing stored."""
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [])
        html = response.content.decode()
        self.assertEqual(text_of(html).count(sentence), 1, sentence)
        form = html[html.index('id="nouvelle-regle"') :]
        form = form[: form.index("</form>")]
        self.assertEqual(form.count('class="field-error"'), errors, text_of(form))
        self.assertEqual(OperationRule.objects.count(), len(SEED.RULES))

    def test_a_name_another_rule_has_whatever_its_case_and_accents_is_refused(self):
        response = self.add(name="dépôt D'ESPECES  (versement especes)")
        self.assertRefused(response, f"La règle « {CASH_RULE} » porte déjà ce nom : choisissez-en un autre.")

    def test_a_name_taken_meanwhile_is_said_on_the_name_never_a_500(self):
        with another_tab_takes_the_name():
            response = self.add()
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        html = response.content.decode()
        form = html[html.index('id="nouvelle-regle"') :]
        self.assertEqual(text_of(form[: form.index("</form>")]).count("Une règle porte déjà ce nom."), 1)
        # The other tab's rule alone: nothing of this one was written.
        self.assertEqual(
            list(OperationRule.objects.filter(name=TPE_RULE).values_list("pattern", flat=True)), ["AUTRE ONGLET"]
        )
        self.assertEqual(OperationRule.objects.count(), len(SEED.RULES) + 1)

    def test_a_pattern_that_is_no_regular_expression_is_refused_in_french(self):
        self.assertRefused(self.add(pattern="REMISE (TPE"), "Motif : parenthèse non fermée (position 8).")

    def test_a_named_group_its_meaning_does_not_read_is_refused(self):
        response = self.add(meaning=Meaning.CASH, pattern="VERSEMENT (?P<tiers>.+)")
        self.assertRefused(
            response, "Motif : le groupe (?P<tiers>…) ne sert à rien ; « Dépôt d'espèces » ne lit aucun groupe nommé."
        )

    def test_a_pattern_finding_an_empty_text_is_refused(self):
        self.assertRefused(
            self.add(pattern="TPE|"),
            "Motif : le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne.",
        )

    def test_what_is_missing_is_said_once_and_only_that(self):
        response = self.add(name="", meaning="", pattern="")
        for sentence in (
            "Donnez un nom à la règle.",
            "Choisissez ce que signifie l'opération trouvée.",
            "Écrivez le motif cherché.",
        ):
            with self.subTest(sentence=sentence):
                self.assertRefused(response, sentence, errors=3)
        # Not the model's own check besides, on the value it held before.
        self.assertNotIn("le motif est vide", text_of(response.content.decode()))

    def test_a_meaning_the_menu_does_not_offer_is_refused(self):
        form = self.new_rule_form()
        data = as_post(form.submission(press=("action", "enregistrer"), values={"name": "X", "pattern": "X"}))
        data["meaning"] = ["autre_chose"]
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choisissez ce que signifie l&#x27;opération trouvée.")
        self.assertEqual(OperationRule.objects.count(), len(SEED.RULES))

    def test_an_unknown_action_writes_nothing(self):
        form = self.new_rule_form()
        data = as_post(
            form.submission(press=("action", "enregistrer"), values={"name": "X", "meaning": "payout", "pattern": "X"})
        )
        data["action"] = ["autre"]
        response = self.client.post(self.url, data, follow=True)
        self.assertEqual(self.messages_of(response), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(OperationRule.objects.count(), len(SEED.RULES))

    def test_a_post_without_its_token_is_refused(self):
        response = self.client.post(
            self.url, {"action": "enregistrer", "name": "X", "meaning": "payout", "searched": "label", "pattern": "X"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OperationRule.objects.count(), len(SEED.RULES))


class TesterTests(Page):
    def setUp(self):
        super().setUp()
        # Twenty payouts of the terminal, the newest last; and a debit
        # printing the same words, which no till rule ever reads.
        self.payouts = [self.terminal(date(2026, 6, day), f"{day}.00", number=day) for day in range(1, 21)]
        self.line(date(2026, 6, 25), "REMISE TPE 999999 FRAIS", "-3.00", "PRELEVEMENT")

    def test_enter_tests_and_never_saves(self):
        for url in (self.url, self.rule_url(seeded(CASH_RULE))):
            with self.subTest(url=url):
                form = form_posting_to(self.html(url), url, holding=("action", "tester"))
                first = form.buttons()[0]
                self.assertEqual((first.name, first.attrs.get("value")), ("action", "tester"))

    def test_tester_counts_shows_the_newest_and_saves_nothing(self):
        before = self.stored()
        response = self.add(press=("action", "tester"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [])
        self.assertEqual(self.stored(), before)
        test = response.context["test"]
        # The credits only - 20 payouts of the terminal and the 2 credits of
        # the statement; the debit of the same words is no credit.
        self.assertEqual((test.till, test.scanned, test.count, test.earlier), (True, 22, 20, 0))
        self.assertEqual(test.total, Decimal(sum(range(1, 21))))
        self.assertEqual(len(test.examples), views.MAX_EXAMPLES)
        self.assertEqual([found.line for found in test.examples], self.payouts[::-1][: views.MAX_EXAMPLES])
        text = text_of(response.content.decode())
        self.assertIn("20 opérations sur 22 entrées d'argent, 210.00 € : rien n'est enregistré.", text)
        self.assertIn("20/06/2026 REMISE TPE 000020 BAR EXEMPLE VIREMENT 20.00 € brut non imprimé (montant reçu)", text)
        self.assertIn("… et 5 de plus.", text)
        self.assertNotIn("déjà décidée", text)
        # What was typed is drawn back.
        form = self.new_rule_form(response.content.decode())
        self.assertEqual(form.control("pattern").value, "REMISE TPE")
        html = response.content.decode()
        self.assertIn('data-sort="2026-06-20"', html)

    def test_tester_reads_the_gross_a_rule_captures(self):
        response = self.add(
            press=("action", "tester"), name="Brut", pattern=r"TOTAL ENCAISSE (?P<encaisse>[0-9]+[.][0-9]{2}) EUROS"
        )
        test = response.context["test"]
        # The seeded payout rule, before it, already reads that line.
        self.assertEqual((test.count, test.earlier), (1, 1))
        self.assertEqual(test.examples[0].gross, Decimal("200.00"))
        text = text_of(response.content.decode())
        self.assertIn("encaissé : 200.00 €", text)
        self.assertIn("Dont 1 déjà décidée par une règle placée avant ou à la main : celle-ci ne les lira pas.", text)

    def test_a_rule_of_what_the_operation_is_is_tried_on_every_line(self):
        response = self.add(
            press=("action", "tester"),
            name="Carte",
            meaning=Meaning.CARD_PAYMENT,
            pattern=r"FACTURE CARTE DU (?P<jour>[0-9]{2})(?P<mois>[0-9]{2})[0-9]{2} (?P<tiers>.+?) CARTE",
        )
        test = response.context["test"]
        self.assertEqual((test.till, test.scanned, test.count, test.earlier), (False, 25, 1, 1))
        self.assertIn(
            "15/07/2026 FACTURE CARTE DU 140726 EPICERIE EXEMPLE CARTE 4974XXXXXXXX1111 FACTURE CARTE -12.30 € "
            "tiers : EPICERIE EXEMPLE ; carte du 14/07/2026",
            text_of(response.content.decode()),
        )

    def test_a_rule_of_what_the_operation_is_says_money_in_and_out_apart(self):
        """A transfer received and one sent of the same amount: their sum is
        0.00 €, which says nothing of what the rule finds."""
        self.line(
            date(2026, 6, 26),
            "VIR SEPA EMIS /BEN BAR EXEMPLE /REFDO 0001",
            "-198.60",
            "VIREMENT",
            kind=TRANSFER,
            counterparty="BAR EXEMPLE",
        )
        response = self.add(press=("action", "tester"), name="Virement", meaning=Meaning.TRANSFER, pattern="^VIR")
        test = response.context["test"]
        self.assertEqual((test.count, test.received, test.paid), (2, Decimal("198.60"), Decimal("198.60")))
        text = text_of(response.content.decode())
        self.assertIn(
            "2 opérations sur 26 opérations importées, 198.60 € reçus, 198.60 € payés : rien n'est enregistré.", text
        )
        self.assertNotIn("importées, 0.00 €", text)

    def test_a_credit_chosen_by_hand_is_one_the_till_rule_will_not_read(self):
        BankTransaction.objects.filter(pk__in=[self.payouts[0].pk, self.payouts[1].pk]).update(income_source="card")
        response = self.add(press=("action", "tester"))
        self.assertEqual((response.context["test"].count, response.context["test"].earlier), (20, 2))
        self.assertIn(
            "Dont 2 déjà décidées par une règle placée avant ou à la main : celle-ci ne les lira pas.",
            text_of(response.content.decode()),
        )

    def test_a_rule_moved_to_the_other_question_has_every_rule_there_before_it(self):
        """« Autre virement (VIR) » tried as « Pas une vente »: saved, it goes
        last of « En caisse », so the payout rule reads the payout first."""
        response = self.edit(seeded(ANY_TRANSFER_RULE), press=("action", "tester"), meaning=Meaning.NOT_A_SALE)
        self.assertEqual((response.context["test"].count, response.context["test"].earlier), (1, 1))

    def test_a_refused_pattern_is_said_and_nothing_is_tried(self):
        response = self.add(press=("action", "tester"), pattern="REMISE (TPE")
        self.assertIsNone(response.context["test"])
        self.assertContains(response, "Motif : parenthèse non fermée (position 8).", count=1)
        self.assertNotContains(response, 'id="essai"')

    def test_the_name_alone_wrong_still_tries_the_rule(self):
        response = self.add(press=("action", "tester"), name=CASH_RULE)
        self.assertEqual(response.context["test"].count, 20)
        self.assertContains(response, "porte déjà ce nom")

    def test_a_rule_too_slow_is_said(self):
        with too_slow("REMISE TPE"):
            response = self.add(press=("action", "tester"))
        self.assertTrue(response.context["test"].slow)
        self.assertEqual(response.context["test"].count, 0)
        self.assertContains(response, "Motif trop lent : il a été arrêté en route, le décompte est incomplet.")

    def test_on_a_rule_s_own_page_only_the_rules_before_it_are_earlier(self):
        # The cheque rule tried on the cash deposit: the cash rule, before
        # it, decides that line already.
        response = self.edit(
            seeded(CHEQUE_RULE), press=("action", "tester"), pattern="VERSEMENT ESPECES", searched="bank_type"
        )
        self.assertEqual((response.context["test"].count, response.context["test"].earlier), (1, 1))
        # The cash rule itself on the same line: nothing before it does.
        response = self.edit(seeded(CASH_RULE), press=("action", "tester"), pattern="ESPECES")
        self.assertEqual((response.context["test"].count, response.context["test"].earlier), (1, 0))
        self.assertEqual(seeded(CASH_RULE).pattern, "VERSEMENT ESPECES")


class EditTests(Page):
    def test_the_rule_s_page_holds_it_as_stored(self):
        url = self.rule_url(seeded(CASH_RULE))
        html = self.html(url)
        form = form_posting_to(html, url, holding=("action", "tester"))
        self.assertEqual(
            [form.control(name).value for name in ("name", "meaning", "searched", "pattern")],
            [CASH_RULE, "cash", "bank_type", "VERSEMENT ESPECES"],
        )
        self.assertIn("Lue à chaque affichage d'« Entrées d'argent ».", text_of(html))

    def test_an_edit_is_saved_and_the_list_says_what_to_read_again(self):
        debit = seeded(DEBIT_RULE)
        response = self.edit(debit, pattern=r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]+)")
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#regle-{debit.pk}")
        self.assertEqual(
            self.messages_of(response),
            [f"Règle « {DEBIT_RULE} » enregistrée : elle vaut pour les relevés importés ensuite."],
        )
        self.assertEqual(seeded(DEBIT_RULE).pattern, r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]+)")
        # The line stored at import is as it was, and the page offers to read it again.
        self.assertEqual(BankTransaction.objects.get(pk=self.debit.pk).counterparty, "FOURNISSEUR EXEMPLE")
        self.assertEqual(response.context["pending"], 1)
        # Counted as « Relire » counts what it would write.
        self.assertEqual(len(recognition.stored_changes().changes), 1)
        self.assertContains(
            response, f'<a href="{reverse("bank:recognition_reapply")}">Relire les opérations déjà importées (1)</a>'
        )

    def test_a_rule_keeps_its_own_name_in_another_case(self):
        self.edit(seeded(CASH_RULE), name="DÉPÔT D'ESPÈCES (versement especes)")
        self.assertTrue(OperationRule.objects.filter(name="DÉPÔT D'ESPÈCES (versement especes)").exists())

    def test_a_refused_edit_writes_nothing_and_the_page_keeps_the_stored_name(self):
        before = self.stored()
        response = self.edit(seeded(CASH_RULE), name=CHEQUE_RULE.upper(), pattern="ESPECES|")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored(), before)
        html = response.content.decode()
        self.assertIn(f"<h1>Règle « {CASH_RULE} »</h1>", unescape(html))
        self.assertEqual(text_of(html).count(f"La règle « {CHEQUE_RULE} » porte déjà ce nom"), 1)
        self.assertEqual(text_of(html).count("le motif accepte une ligne vide"), 1)

    def test_a_rule_moved_to_the_other_question_goes_last_of_it(self):
        highest = OperationRule.objects.order_by("-position").first().position
        cheque = seeded(CHEQUE_RULE)
        response = self.edit(cheque, meaning=Meaning.OTHER_OPERATION, searched="label", pattern="REMISE CHEQUE")
        self.assertEqual(
            self.messages_of(response),
            [
                (
                    f"Règle « {CHEQUE_RULE} » enregistrée, dernière de sa nouvelle partie : elle vaut pour les "
                    "relevés importés ensuite."
                )
            ],
        )
        self.assertEqual(seeded(CHEQUE_RULE).position, highest + 1)
        self.assertEqual(self.order(response), [*KIND_RULES, CHEQUE_RULE])
        self.assertEqual(self.order(response, "till_rows"), [PAYOUT_RULE, CASH_RULE])

    def test_a_kind_rule_moved_to_the_till_goes_last_and_the_payout_still_reads_card(self):
        """« Autre virement (VIR) » (position 5) made « Pas une vente »: kept
        at 5 it came FIRST of the till rules, before the payout rule, and
        every card payout read « Pas une vente »."""
        self.edit(seeded(ANY_TRANSFER_RULE), meaning=Meaning.NOT_A_SALE)
        response = self.get()
        self.assertEqual(self.order(response, "till_rows"), [*TILL_RULES, ANY_TRANSFER_RULE])
        self.assertEqual(self.order(response), KIND_RULES[:-1])
        payout = BankTransaction.objects.get(pk=self.payout.pk)
        entry = income.entry_for(payout, None, recognition.load())
        self.assertEqual((entry.source, entry.gross, entry.rule), ("card", Decimal("200.00"), PAYOUT_RULE))

    def test_a_rule_kept_in_its_question_keeps_its_place(self):
        position = seeded(CASH_RULE).position
        self.edit(seeded(CASH_RULE), pattern="ESPECES")
        self.assertEqual(seeded(CASH_RULE).position, position)
        self.assertEqual(self.order(self.get(), "till_rows"), TILL_RULES)

    def test_a_name_taken_meanwhile_is_said_on_the_name_never_a_500(self):
        cash = seeded(CASH_RULE)
        with another_tab_takes_the_name():
            response = self.edit(cash, name="Dépôt en banque")
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertEqual(text_of(response.content.decode()).count("Une règle porte déjà ce nom."), 1)
        self.assertEqual(seeded(CASH_RULE).pk, cash.pk)
        self.assertEqual(OperationRule.objects.get(name="Dépôt en banque").pattern, "AUTRE ONGLET")


class MoveTests(Page):
    def positions(self) -> dict:
        return dict(OperationRule.objects.values_list("name", "position"))

    def test_up_and_down_swap_a_rule_with_its_neighbour_of_its_question(self):
        response = self.press(seeded(CASH_RULE), "monter")
        self.assertEqual(self.messages_of(response), [f"Règle « {CASH_RULE} » montée."])
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#regle-{seeded(CASH_RULE).pk}")
        self.assertEqual(self.order(response, "till_rows"), [CASH_RULE, PAYOUT_RULE, CHEQUE_RULE])
        self.assertEqual(self.order(response), KIND_RULES)
        response = self.press(seeded(CASH_RULE), "descendre")
        self.assertEqual(self.messages_of(response), [f"Règle « {CASH_RULE} » descendue."])
        self.assertEqual(self.order(response, "till_rows"), TILL_RULES)

    def test_the_ends_of_a_question_are_its_own(self):
        html = self.html()
        # The first rule of each question cannot go up, the last down - the
        # button is drawn disabled, and posted all the same it moves nothing.
        for name, action in (
            (CARD_RULE, "monter"),
            (ANY_TRANSFER_RULE, "descendre"),
            (PAYOUT_RULE, "monter"),
            (CHEQUE_RULE, "descendre"),
        ):
            with self.subTest(rule=name, action=action):
                form = self.button_form(seeded(name), action, html)
                with self.assertRaises(AssertionError):
                    form.submission(press=("action", action))
        positions = self.positions()
        token = form_posting_to(html, self.url).control("csrfmiddlewaretoken").value
        response = self.client.post(
            self.rule_url(seeded(ANY_TRANSFER_RULE)), {"action": "descendre", "csrfmiddlewaretoken": token}, follow=True
        )
        self.assertEqual(self.messages_of(response), [f"Règle « {ANY_TRANSFER_RULE} » déjà la dernière de sa partie."])
        response = self.client.post(
            self.rule_url(seeded(PAYOUT_RULE)), {"action": "monter", "csrfmiddlewaretoken": token}, follow=True
        )
        self.assertEqual(self.messages_of(response), [f"Règle « {PAYOUT_RULE} » déjà la première de sa partie."])
        # The last rule of what an operation is never trades places with the
        # first of the till, whose position comes next.
        self.assertEqual(self.positions(), positions)

    def test_rules_sharing_a_position_are_ordered_and_moved_one_at_a_time(self):
        OperationRule.objects.filter(name__in=TILL_RULES).update(position=0)
        response = self.get()
        # Rules of one position are asked by name, never by id.
        self.assertEqual(self.order(response, "till_rows"), [CASH_RULE, CHEQUE_RULE, PAYOUT_RULE])
        self.assertEqual(sorted(TILL_RULES), [CASH_RULE, CHEQUE_RULE, PAYOUT_RULE])
        kinds = {name: position for name, position in self.positions().items() if name in KIND_RULES}
        response = self.press(seeded(PAYOUT_RULE), "monter")
        self.assertEqual(self.order(response, "till_rows"), [CASH_RULE, PAYOUT_RULE, CHEQUE_RULE])
        positions = self.positions()
        self.assertEqual(sorted(positions[name] for name in TILL_RULES), [0, 1, 2])
        self.assertEqual({name: positions[name] for name in KIND_RULES}, kinds)
        response = self.press(seeded(PAYOUT_RULE), "monter")
        self.assertEqual(self.order(response, "till_rows"), [PAYOUT_RULE, CASH_RULE, CHEQUE_RULE])

    def test_the_order_is_the_order_the_import_reads(self):
        """« Virement reçu (/FRM) » moved under « Autre virement (VIR) »: a
        transfer received is then read by the rule that reads no payee."""
        self.press(seeded(RECEIVED_RULE), "descendre")
        found = recognition.describe(
            recognition.load(), self.payout.label, self.payout.bank_type, self.payout.operation_date
        )
        self.assertEqual((found.rule, found.counterparty), (ANY_TRANSFER_RULE, ""))


class StateTests(Page):
    def test_suspend_then_reactivate(self):
        response = self.press(seeded(CASH_RULE), "suspendre")
        self.assertEqual(self.messages_of(response), [f"Règle « {CASH_RULE} » suspendue."])
        self.assertFalse(seeded(CASH_RULE).is_active)
        self.assertIn("Suspendue", text_of(row_of(response.content.decode(), seeded(CASH_RULE))))
        response = self.press(seeded(CASH_RULE), "reactiver")
        self.assertEqual(self.messages_of(response), [f"Règle « {CASH_RULE} » réactivée."])
        self.assertTrue(seeded(CASH_RULE).is_active)

    def test_delete_asks_first_then_deletes(self):
        cash = seeded(CASH_RULE)
        form = self.button_form(cash, "supprimer")
        self.assertEqual(form.attrs["data-confirm"], f"Supprimer la règle « {CASH_RULE} » ?")
        response = self.send(form, press=("action", "supprimer"))
        self.assertEqual(self.messages_of(response), [f"Règle « {CASH_RULE} » supprimée."])
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#en-caisse")
        self.assertFalse(OperationRule.objects.filter(pk=cash.pk).exists())
        self.assertEqual(self.order(response, "till_rows"), [PAYOUT_RULE, CHEQUE_RULE])

    def test_a_get_writes_nothing(self):
        before = self.stored()
        cash = seeded(CASH_RULE)
        for action in ("supprimer", "suspendre", "monter", "descendre", "enregistrer"):
            with self.subTest(action=action):
                response = self.client.get(self.rule_url(cash), {"action": action, "pattern": "X"})
                self.assertEqual(response.status_code, 200)
        self.get(reverse("bank:recognition_reapply"))
        self.get(self.url + "?action=enregistrer&name=X&meaning=payout&searched=label&pattern=X")
        self.assertEqual(self.stored(), before)

    def test_an_unknown_action_or_none_writes_nothing(self):
        before = self.stored()
        token = self.new_rule_form().control("csrfmiddlewaretoken").value
        for action in ("autre", None):
            with self.subTest(action=action):
                data = {"csrfmiddlewaretoken": token, **({"action": action} if action else {})}
                response = self.client.post(self.rule_url(seeded(CASH_RULE)), data, follow=True)
                self.assertEqual(self.messages_of(response), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(self.stored(), before)

    def test_a_rule_that_is_not_there_is_a_404(self):
        url = reverse("bank:recognition_rule", args=[999999])
        self.get(url, status=404)
        token = self.new_rule_form().control("csrfmiddlewaretoken").value
        self.assertEqual(self.client.post(url, {"action": "supprimer", "csrfmiddlewaretoken": token}).status_code, 404)


class ReapplyTests(Page):
    def setUp(self):
        super().setUp()
        self.REAPPLY = reverse("bank:recognition_reapply")

    def cut_the_debit_payee(self, pattern=r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]+)"):
        OperationRule.objects.filter(name=DEBIT_RULE).update(pattern=pattern)

    def preview_form(self):
        return form_posting_to(self.html(self.REAPPLY), self.REAPPLY)

    def test_nothing_to_read_again(self):
        response = self.get(self.REAPPLY)
        self.assertContains(
            response, "Rien à relire : les opérations importées sont déjà lues ainsi par les règles actives."
        )
        self.assertNotContains(response, 'name="empreinte"')

    def test_the_preview_says_what_changes_then_the_post_writes_it(self):
        self.cut_the_debit_payee()
        OperationRule.objects.filter(name=CARD_RULE).update(pattern=r"FACTURE CARTE DU [0-9]{6} (?P<tiers>.*?)\s+CARTE")
        OperationRule.objects.filter(name=RECEIVED_RULE).update(is_active=False)
        before = self.stored()
        html = self.html(self.REAPPLY)
        self.assertEqual(self.stored(), before)
        text = text_of(html)
        self.assertIn("3 opérations changent.", text)
        self.assertIn("198.60 € Virement BAR EXEMPLE → — —", text)
        self.assertIn("09/07/2026", text)
        self.assertIn("Prélèvement FOURNISSEUR EXEMPLE → FOURNISSEUR —", text)
        self.assertIn("Carte EPICERIE EXEMPLE 14/07/2026 → —", text)
        response = self.send(form_posting_to(html, self.REAPPLY))
        self.assertEqual(self.messages_of(response), ["3 opérations relues."])
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#nature")
        self.assertEqual(
            list(
                BankTransaction.objects.filter(pk__in=[self.card.pk, self.debit.pk, self.payout.pk])
                .order_by("pk")
                .values_list("kind", "counterparty", "card_date")
            ),
            [(CARD, "EPICERIE EXEMPLE", None), (DEBIT, "FOURNISSEUR", None), (TRANSFER, "", None)],
        )
        self.assertEqual(response.context["pending"], 0)

    def test_a_rule_changed_since_the_preview_writes_nothing(self):
        self.cut_the_debit_payee()
        form = self.preview_form()
        self.cut_the_debit_payee(r"^PRLV SEPA (?:B2B )?(?P<tiers>[^ ]{3})")
        before = self.stored()
        response = self.send(form)
        self.assertEqual(self.stored(), before)
        self.assertEqual(response.redirect_chain[-1][0], self.REAPPLY)
        self.assertEqual(
            self.messages_of(response),
            ["Rien n'a changé : les règles ou les opérations ont changé depuis l'aperçu. Vérifiez le nouvel aperçu."],
        )
        # The new preview says what the rules read now.
        self.assertIn("FOURNISSEUR EXEMPLE → FOU", text_of(response.content.decode()))

    def test_a_rule_that_cannot_be_applied_writes_nothing_and_says_why(self):
        self.cut_the_debit_payee()
        form = self.preview_form()
        OperationRule.objects.filter(name=CHEQUE_RULE).update(pattern="(")
        before = self.stored()
        response = self.send(form)
        self.assertEqual(self.stored(), before)
        self.assertEqual(
            self.messages_of(response),
            [
                (
                    f"Rien n'a changé : la règle de reconnaissance « {CHEQUE_RULE} » ne peut pas être appliquée "
                    "(motif invalide ou trop lent). Corrigez-la sur « Reconnaissance des opérations », puis relisez "
                    "les opérations."
                )
            ],
        )
        # And the preview offers nothing to write while it stands.
        html = self.html(self.REAPPLY)
        self.assertNotIn('name="empreinte"', html)
        self.assertIn("Rien ne peut être relu tant qu'une règle active ne peut pas être appliquée", text_of(html))
        self.assertIn(
            f"Règle « {CHEQUE_RULE} » : parenthèse non fermée (position 1) - elle ne reconnaît rien.", text_of(html)
        )

    def test_the_card_date_sorts_by_its_date(self):
        """« 14/07/2026 » sorts as text by its day, whatever the year."""
        OperationRule.objects.filter(name=CARD_RULE).update(
            pattern=r"FACTURE CARTE DU (?P<jour>[0-9]{2})(?P<mois>[0-9]{2})(?P<annee>[0-9]{2}) (?P<tiers>[^ ]+)"
        )
        self.cut_the_debit_payee()
        table = table_of(self.html(self.REAPPLY), "opérations relues")
        cells = re.findall(r'<td data-sort="([^"]*)">\s*([^<]*?)\s*</td>', table)
        # The card payment's card date, unchanged; the debit has none.
        self.assertIn(("2026-07-14", "14/07/2026"), cells)
        self.assertIn(("", "—"), cells)

    def test_the_preview_says_which_credits_stop_following_their_payer(self):
        """A payee capture shortened: « TERMINAL EXEMPLE » becomes « TERMINAL »,
        and the two credits that card payouts only through their retained payer
        stop following it - no field of theirs is written, yet they leave the
        card figures. Said before, with the debit whose learnt alias no longer
        names its payee."""
        first, second = (
            self.line(
                date(2026, 7, day),
                f"VIR SEPA RECU /FRM TERMINAL EXEMPLE /REF 000{day} REMISE",
                amount,
                "VIREMENT",
                kind=TRANSFER,
                counterparty="TERMINAL EXEMPLE",
            )
            for day, amount in ((1, "245.50"), (2, "120.00"))
        )
        own = self.line(
            date(2026, 7, 4),
            "VIR SEPA RECU /FRM TERMINAL EXEMPLE /REF 0004 REMBOURSEMENT",
            "15.00",
            "VIREMENT",
            kind=TRANSFER,
            counterparty="TERMINAL EXEMPLE",
        )
        income.set_source(first, income.CARD, remember=True)
        BankTransaction.objects.filter(pk=own.pk).update(income_source=income.OTHER)
        # The payout is the payout rule's whoever pays it: its payer retained
        # follows nothing.
        IncomePayer.objects.create(key="BAR EXEMPLE", source=income.CASH)
        supplier = Supplier.objects.create(code="FOURN-EX", name="Fournisseur Exemple")
        CounterpartyAlias.objects.create(supplier=supplier, name="FOURNISSEUR EXEMPLE")
        rules = recognition.load()
        self.assertEqual(
            [income.reading_of(line, income.known_payers(), rules=rules).how for line in (first, second)],
            [income.BY_PAYER, income.BY_PAYER],
        )
        OperationRule.objects.filter(name=RECEIVED_RULE).update(pattern=r"^VIR.*?/FRM (?P<tiers>\S+)")
        self.cut_the_debit_payee()

        response = self.get(self.REAPPLY)
        self.assertEqual((response.context["detached_payers"], response.context["detached_aliases"]), (2, 1))
        text = text_of(response.content.decode())
        self.assertIn("5 opérations changent.", text)
        self.assertIn("TERMINAL EXEMPLE → TERMINAL", text)
        self.assertIn(
            "Les rapprochements, les catégories et le choix « En caisse » propre à chaque opération restent tels "
            "quels.",
            text,
        )
        self.assertIn("2 entrées ne suivront plus leur payeur retenu : à reclasser sur « Entrées d'argent »", text)
        self.assertIn("1 dépense ne correspondra plus au libellé bancaire appris pour son fournisseur.", text)

        # And it holds: once read again, nothing follows the payer any more.
        response = self.send(form_posting_to(response.content.decode(), self.REAPPLY))
        self.assertEqual(self.messages_of(response), ["5 opérations relues."])
        rules = recognition.load()
        read = [
            income.reading_of(BankTransaction.objects.get(pk=line.pk), income.known_payers(), rules=rules)
            for line in (first, second, own)
        ]
        self.assertEqual(
            [(one.source, one.how) for one in read],
            [(income.OTHER, income.BY_RULE), (income.OTHER, income.BY_RULE), (income.OTHER, income.BY_LINE)],
        )

    def test_a_payee_unchanged_unties_nothing(self):
        income.set_source(self.payout, income.CARD, remember=True)
        OperationRule.objects.filter(name=CARD_RULE).update(pattern=r"FACTURE CARTE DU [0-9]{6} (?P<tiers>.*?)\s+CARTE")
        response = self.get(self.REAPPLY)
        self.assertEqual((response.context["detached_payers"], response.context["detached_aliases"]), (0, 0))
        self.assertNotContains(response, "payeur retenu")

    def test_a_long_preview_is_cut_and_says_how_many_more(self):
        self.cut_the_debit_payee()
        for day in range(1, 5):
            self.line(
                date(2026, 8, day),
                f"PRLV SEPA AUTRE FOURNISSEUR ECH/0{day}0826",
                "-5.00",
                kind=DEBIT,
                counterparty="AUTRE FOURNISSEUR",
            )
        with mock.patch.object(views, "MAX_CHANGES_SHOWN", 2):
            response = self.get(self.REAPPLY)
        self.assertEqual(len(response.context["rows"]), 2)
        text = text_of(response.content.decode())
        self.assertIn("5 opérations changent.", text)
        self.assertIn("… et 3 de plus.", text)
        self.assertIn("Relire 5 opérations", text)
