"""« Partir d'un modèle » (bank/presets.py, « Format du relevé » `#modeles`):
a format and its recognition rules added in one click.

Every preset must pass the checks a person's own format and rules pass - a
preset failing them would be a page refusing its own button, and (step 8)
a signup failing. The BNP preset is the migrations' literals, never a copy.
Installing writes only what is missing, keeps whatever is there under the
same name whatever its spelling, and writes once however often it is
pressed. And a preset reads its kind of file end to end: the OFX and
CAMT.053 fixtures' card payments, debits and transfers come out as such.

Every name, code and amount in the fixtures is invented.
"""

from __future__ import annotations

import importlib
from unittest import mock

from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bank import presets, recognition, reconcile, statements, views
from bank.models import BankTransaction, OperationRule, StatementFormat
from bank.tests import camt_files, ofx_files
from bank.tests.support import SEEDED_NAMES, pause_seeded_rules
from bank.tests.test_recognition_views import text_of
from staff.tests.page_forms import as_post, form_posting_to
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

SEEDED_FORMAT = importlib.import_module("bank.migrations.0007_statement_formats")
SEEDED_RULES = importlib.import_module("bank.migrations.0006_operation_rules")


def stored() -> list:
    """Every format and rule as stored, by name."""
    return [
        sorted(StatementFormat.objects.values_list("name", "position", "file_type")),
        sorted(OperationRule.objects.values_list("name", "position", "meaning", "searched", "pattern", "is_active")),
    ]


class PresetCheckTests(SimpleTestCase):
    """Each preset passes the very checks the page's forms run."""

    def test_every_format_passes_the_format_check(self):
        for preset in presets.PRESETS:
            with self.subTest(preset=preset.key):
                layout = statements.check_format(
                    StatementFormat(name=preset.format_name, position=1, **preset.format_fields)
                )
                self.assertEqual(layout.name, preset.format_name)

    def test_every_rule_passes_the_pattern_guard_with_its_meaning(self):
        for preset in presets.PRESETS:
            for name, meaning, searched, pattern in preset.rules:
                with self.subTest(preset=preset.key, rule=name):
                    recognition.check(meaning, searched, pattern)
                    self.assertIn(searched, OperationRule.Searched.values)
                    self.assertLessEqual(len(name), 100)

    def test_names_are_unique_within_and_across_presets(self):
        names = [recognition.name_key(name) for preset in presets.PRESETS for name, *_rest in preset.rules]
        self.assertEqual(len(names), len(set(names)))
        formats = [recognition.name_key(preset.format_name) for preset in presets.PRESETS]
        self.assertEqual(len(formats), len(set(formats)))

    def test_the_bnp_preset_is_the_migrations_literals(self):
        bnp = presets.BY_KEY["bnp-csv"]
        self.assertEqual(bnp.format_name, SEEDED_FORMAT.NAME)
        self.assertEqual(bnp.format_fields, {k: v for k, v in SEEDED_FORMAT.FORMAT.items() if k != "position"})
        self.assertEqual(list(bnp.rules), [rule[1:] for rule in SEEDED_RULES.RULES])

    def test_the_presets_are_offered_in_their_order(self):
        self.assertEqual([preset.key for preset in presets.PRESETS], ["ofx", "camt053", "bnp-csv"])
        self.assertEqual(
            [(preset.format_fields.get("file_type", "csv")) for preset in presets.PRESETS], ["ofx", "camt053", "csv"]
        )


class InstallTests(TestCase):
    def test_a_preset_s_format_comes_last_and_its_rules_after_every_rule(self):
        highest_rule = OperationRule.objects.order_by("-position").first().position
        done = presets.install(presets.OFX)
        self.assertEqual(done.kept, [])
        self.assertEqual((done.format.name, done.format.position, done.format.file_type), ("Relevé OFX", 2, "ofx"))
        self.assertEqual(
            [(rule.name, rule.position) for rule in done.rules],
            [
                ("Carte (OFX : POS)", highest_rule + 1),
                ("Prélèvement (OFX : DIRECTDEBIT)", highest_rule + 2),
                ("Virement (OFX : XFER, DIRECTDEP)", highest_rule + 3),
            ],
        )
        # The owner's format is still the import's default.
        self.assertEqual(reconcile.default_format().name, SEEDED_FORMAT.NAME)

    def test_installing_twice_writes_once(self):
        presets.install(presets.CAMT053)
        before = stored()
        again = presets.install(presets.CAMT053)
        self.assertFalse(again.wrote)
        self.assertEqual(len(again.kept), 1 + len(presets.CAMT053.rules))
        self.assertEqual(stored(), before)

    def test_the_owner_s_bank_is_here_already(self):
        """Every database holds the BNP seeds: nothing is written."""
        before = stored()
        done = presets.install(presets.BNP_CSV)
        self.assertFalse(done.wrote)
        self.assertEqual(done.kept, [SEEDED_FORMAT.NAME, *SEEDED_NAMES])
        self.assertEqual(stored(), before)

    def test_a_name_here_whatever_its_spelling_is_kept_as_it_is(self):
        mine = StatementFormat.objects.create(
            name="relevé  ofx", position=9, file_type="csv", date_column=1, label_columns="2", amount_column=3
        )
        OperationRule.objects.create(
            name="CARTE (ofx : pos)", meaning="debit", searched="label", pattern="^MOI", position=50
        )
        done = presets.install(presets.OFX)
        self.assertIsNone(done.format)
        self.assertEqual(done.kept, ["relevé  ofx", "CARTE (ofx : pos)"])
        mine.refresh_from_db()
        self.assertEqual((mine.file_type, mine.position, mine.date_column), ("csv", 9, 1))
        self.assertEqual(OperationRule.objects.get(name="CARTE (ofx : pos)").pattern, "^MOI")
        self.assertEqual(
            [rule.name for rule in done.rules], ["Prélèvement (OFX : DIRECTDEBIT)", "Virement (OFX : XFER, DIRECTDEP)"]
        )

    def test_into_an_empty_bank_its_format_is_the_default(self):
        StatementFormat.objects.all().delete()
        OperationRule.objects.all().delete()
        done = presets.install(presets.BNP_CSV)
        self.assertEqual((done.format.position, len(done.rules)), (1, len(SEEDED_RULES.RULES)))
        self.assertEqual(reconcile.default_format().name, SEEDED_FORMAT.NAME)
        self.assertEqual(recognition.load().invalid, {})


class EndToEndTests(TestCase):
    """A preset reads its kind of file: what each operation is comes from
    the preset's rules alone (the seeded ones paused)."""

    def setUp(self):
        super().setUp()
        pause_seeded_rules()

    def kinds(self, preset, content) -> list[str]:
        fmt = presets.install(preset).format
        reconcile.import_statement(content, recognition.load(), fmt)
        return list(BankTransaction.objects.order_by("operation_date", "pk").values_list("kind", flat=True))

    def test_ofx(self):
        self.assertEqual(
            self.kinds(presets.OFX, ofx_files.sgml()),
            ["CARD", "CARD", "TRANSFER", "DEBIT", "OTHER"],
        )

    def test_camt053(self):
        self.assertEqual(
            self.kinds(presets.CAMT053, camt_files.v08()),
            ["CARD", "CARD", "TRANSFER", "DEBIT", "TRANSFER", "OTHER"],
        )


class PageTests(TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_class(enforce_csrf_checks=True)
        self.url = reverse("bank:statement_formats")

    def html(self, url=None) -> str:
        response = self.client.get(url or self.url)
        self.assertEqual(response.status_code, 200)
        assertNoUnrenderedTemplateSyntax(self, response, url or self.url)
        return response.content.decode()

    def press(self, key, html=None):
        form = form_posting_to(html or self.html(), self.url, holding=("modele", key))
        return self.client.post(self.url, as_post(form.submission(press=("action", "modele"))), follow=True)

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def test_the_card_offers_every_preset_and_says_which_is_here(self):
        html = self.html()
        card = text_of(html[html.index('id="modeles"') : html.index('id="nouveau-format"')])
        self.assertIn("Partir d'un modèle", card)
        for preset in presets.PRESETS:
            with self.subTest(preset=preset.key):
                self.assertIn(f'aria-label="Ajouter le modèle « {preset.title} »"', html)
                self.assertIn(preset.description, card)
        self.assertIn("BNP Paribas (CSV) déjà ajouté", card)
        self.assertNotIn("Relevé OFX déjà ajouté", card)

    def test_a_preset_added_from_the_page(self):
        response = self.press("ofx")
        made = StatementFormat.objects.get(name="Relevé OFX")
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#format-{made.pk}")
        self.assertEqual(
            self.messages_of(response),
            [
                (
                    "Modèle « Relevé OFX » : format « Relevé OFX » ajouté, 3 règle(s) de reconnaissance "
                    "ajoutée(s), dernière(s) de leur partie : elles valent pour les relevés importés ensuite."
                )
            ],
        )
        self.assertIn("Relevé OFX déjà ajouté", text_of(response.content.decode()))

    def test_a_preset_here_already_writes_nothing_and_says_so(self):
        before = stored()
        response = self.press("bnp-csv")
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#modeles")
        self.assertEqual(
            self.messages_of(response),
            [
                "Modèle « BNP Paribas (CSV) » : rien à ajouter. 9 noms déjà là (le format ou des règles), gardés tels quels."
            ],
        )
        self.assertEqual(stored(), before)

    def test_its_post_carries_no_format_field_and_needs_none(self):
        """Answered before « Nouveau format » is read: no field error."""
        form = form_posting_to(self.html(), self.url, holding=("modele", "camt053"))
        self.assertEqual(sorted(form.names), ["action", "csrfmiddlewaretoken", "modele"])
        self.press("camt053")
        self.assertTrue(StatementFormat.objects.filter(name="Relevé CAMT.053").exists())

    def test_an_unknown_preset_writes_nothing(self):
        before = stored()
        form = form_posting_to(self.html(), self.url, holding=("modele", "ofx"))
        for key in ("qif", "", "OFX", "ofx "):
            with self.subTest(key=key):
                data = as_post(form.submission(press=("action", "modele")))
                data["modele"] = [key]
                response = self.client.post(self.url, data, follow=True)
                self.assertEqual(self.messages_of(response), [views.UNKNOWN_PRESET])
        self.assertEqual(stored(), before)

    def test_a_name_taken_at_the_same_moment_writes_nothing_never_a_500(self):
        before = stored()
        with mock.patch.object(presets, "install", side_effect=IntegrityError("UNIQUE constraint failed")):
            response = self.press("ofx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.messages_of(response), [views.PRESET_RACED])
        self.assertEqual(stored(), before)

    def test_a_get_writes_nothing(self):
        before = stored()
        self.html()
        self.assertEqual(stored(), before)

    def test_banque_s_empty_state_and_recognition_lead_to_the_presets(self):
        target = f"{self.url}#modeles"
        self.assertIn(f'<a href="{target}">Partir d\'un modèle</a>', self.html(reverse("bank:bank_home")))
        self.assertIn(f'<a href="{target}">Ajouter les règles d\'un modèle</a>', self.html(reverse("bank:recognition")))
        StatementFormat.objects.all().delete()
        self.assertIn('<a href="#modeles">partez d\'un modèle</a>', self.html())

    def test_the_new_format_s_form_still_saves_beside_the_presets(self):
        form = form_posting_to(self.html(), self.url, holding=("action", "tester"))
        values = {
            "name": "Relevé OFX maison",
            "file_type": "ofx",
        }
        data = as_post(form.submission(press=("action", "enregistrer"), values=values))
        response = self.client.post(self.url, data, follow=True)
        self.assertEqual(self.messages_of(response), ["Format « Relevé OFX maison » ajouté."])
        self.assertEqual(StatementFormat.objects.get(name="Relevé OFX maison").date_column, None)
