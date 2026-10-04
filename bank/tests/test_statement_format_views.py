"""« Format du relevé » (`/banque/format/`, bank/views.py: `statement_formats`,
`statement_format`) as the owner uses it - every form read off the page it
drew and posted as a browser posts it (staff/tests/page_forms.py), its CSRF
token checked:

* the list shows the formats in their order, the first being the import's
  default, with which column holds what; a stored format the check refuses
  is said on its row and on its page;
* a new format is checked before it is stored (its name whatever its case
  and accents, every column, the amount said once, the account pattern by
  the guard), each refusal said once, in French, on its own field - and
  comes last;
* « Tester » reads a file picked on the page with the format as TYPED and
  saves nothing, keeps nothing: the file's first rows in numbered columns -
  what a person picks the numbers from - then the operations the format
  reads there, or the sentence the import would refuse it with;
* a format is edited, moved up or down, deleted - never the last one - and
  nothing but a POST writes.

Every label, payee, account number and amount below is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from bank import recognition, reconcile, statements, views
from bank.forms import StatementFormatForm
from bank.models import BankTransaction, StatementFormat
from bank.tests import ofx_files
from bank.tests.support import FORMAT_SEED, make_format
from bank.tests.test_recognition_views import table_of, text_of
from bank.tests.test_reconcile import card_row, debit_row, statement
from staff.tests.page_forms import as_post, form_posting_to
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

SEEDED = FORMAT_SEED.NAME
#: What a NUL typed in a text field is told, in French.
NUL_SAID = "Caractère interdit (NUL) : retapez ce champ."

#: Another bank's export, invented: commas, a header of column names, ISO
#: dates, English decimals, debits and credits apart, the label over two
#: columns, the account above the operations.
OTHER_BANK = (
    "Banque Exemple - export des opérations\n"
    "Compte,000123456789\n"
    "Date,Libellé,Détail,Débit,Crédit\n"
    "2026-07-03,CB EPICERIE EXEMPLE,ref 0001,12.30,\n"
    '2026-07-05,VIR CLIENT EXEMPLE,ref 0002,,"1,250.00"\n'
).encode()
#: The format that reads it, as typed on the page.
OTHER_FORMAT = {
    "name": "Banque Exemple (CSV)",
    "encoding": "auto",
    "delimiter": ",",
    "date_format": "yyyy-mm-dd",
    "decimal_mark": ".",
    "date_column": "1",
    "label_columns": "2,3",
    "amount_column": "",
    "debit_column": "4",
    "credit_column": "5",
    "value_date_column": "",
    "bank_type_column": "",
    "account_pattern": r"^Compte,(?P<compte>[0-9]+)",
}
#: The same, as `make_format` stores it.
OTHER_FIELDS = {
    "delimiter": ",",
    "date_format": "yyyy-mm-dd",
    "decimal_mark": ".",
    "date_column": 1,
    "label_columns": "2, 3",
    "debit_column": 4,
    "credit_column": 5,
    "account_pattern": r"^Compte,(?P<compte>[0-9]+)",
}


def bnp_file() -> bytes:
    """The owner's bank's export, invented: a card payment and a direct debit."""
    return statement(
        card_row(date(2026, 7, 15), "EPICERIE EXEMPLE", "13,06"),
        debit_row(date(2026, 7, 9), "FOURNISSEUR EXEMPLE", "120,35"),
    )


def upload(content: bytes, name="releve.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/csv")


def seeded() -> StatementFormat:
    return StatementFormat.objects.get(name=SEEDED)


def field_of(html: str, name: str) -> str:
    """The `.form-field` block holding the control `name` - its label, the
    control, its help and its errors."""
    at = html.index(f'name="{name}"')
    start = html.rindex('<div class="form-field', 0, at)
    return html[start : html.index("</div>", at)]


def row_of(html: str, fmt: StatementFormat) -> str:
    start = html.index(f'<tr id="format-{fmt.pk}">')
    return html[start : html.index("</tr>", start)]


def button_tag(row: str, value: str) -> str:
    """The opening tag of the row's button posting `value`."""
    at = row.index(f'value="{value}"')
    return row[row.rindex("<button", 0, at) : row.index(">", at)]


def another_tab_takes_the_name():
    """The name the form has just checked, saved by another request before
    this one writes (two tabs, a double click)."""
    real = StatementFormatForm.clean_name

    def racing(form):
        name = real(form)
        make_format(name, **OTHER_FIELDS)
        return name

    return mock.patch.object(StatementFormatForm, "clean_name", racing)


class Page(TestCase):
    def setUp(self):
        super().setUp()
        # The suite's client, logged in, CSRF enforced as a browser's is.
        self.client = self.client_class(enforce_csrf_checks=True)
        self.url = reverse("bank:statement_formats")

    def format_url(self, fmt) -> str:
        return reverse("bank:statement_format", args=[fmt.pk])

    def get(self, url=None, status=200):
        url = url or self.url
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        if status == 200:
            assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def html(self, url=None) -> str:
        return self.get(url).content.decode()

    def send(self, form, *, press=None, values=None, file=None, follow=True):
        """`form` submitted as the browser would, with `file` picked in its
        « Tester sur un fichier »."""
        data = as_post(form.submission(press=press, values=values))
        if file is not None:
            self.assertIn(views.TEST_FILE, form.names)
            # The test client posts multipart whenever it is handed a file,
            # whatever the page says; a browser sends the file only when the
            # form asks for it, and « Tester » otherwise reads nothing.
            self.assertEqual(form.attrs.get("enctype"), "multipart/form-data", form.action)
            data[views.TEST_FILE] = file
        response = self.client.post(form.action.split("#")[0], data, follow=follow)
        if follow and response.status_code == 200:
            assertNoUnrenderedTemplateSyntax(self, response, form.action)
        return response

    def new_form(self, html=None):
        return form_posting_to(html or self.html(), self.url)

    def add(self, *, press=("action", "enregistrer"), file=None, **values):
        """The « Nouveau format » card, filled in and pressed."""
        return self.send(self.new_form(), press=press, values={**OTHER_FORMAT, **values}, file=file)

    def edit_form(self, fmt):
        url = self.format_url(fmt)
        return form_posting_to(self.html(url), url, holding=("action", "tester"))

    def edit(self, fmt, *, press=("action", "enregistrer"), file=None, **values):
        """The format's own page, its form changed and pressed."""
        return self.send(self.edit_form(fmt), press=press, values=values, file=file)

    def button_form(self, fmt, action, html=None):
        """The list's form posting `action` on `fmt`."""
        return form_posting_to(html or self.html(), self.format_url(fmt), holding=("action", action))

    def press(self, fmt, action):
        return self.send(self.button_form(fmt, action), press=("action", action))

    def token(self) -> str:
        return self.new_form().control("csrfmiddlewaretoken").value

    def messages_of(self, response) -> list[str]:
        return [str(message) for message in response.context["messages"]]

    def order(self, response=None) -> list[str]:
        return [row.fmt.name for row in (response or self.get()).context["rows"]]

    def stored(self) -> list:
        """Every format as stored, and every line imported."""
        return [
            list(
                StatementFormat.objects.order_by("pk").values_list(
                    "name", "position", "delimiter", "date_column", "label_columns", "amount_column", "account_pattern"
                )
            ),
            list(BankTransaction.objects.values_list("fingerprint", flat=True)),
        ]


class ListTests(Page):
    def test_the_seeded_format_is_listed_and_is_the_default(self):
        response = self.get()
        self.assertEqual(self.order(response), [SEEDED])
        row = text_of(row_of(response.content.decode(), seeded()))
        self.assertEqual(
            row,
            f"1 ↑ ↓ {SEEDED} par défaut CSV (colonnes) date 1 · libellé 4 · montant 6 · valeur 5 · type 2 "
            "Point-virgule ( ; ) "
            "jj/mm/aaaa Virgule (1 234,56) Modifier Supprimer",
        )
        self.assertNotContains(response, "à corriger")

    def test_formats_are_listed_in_their_order_the_first_alone_by_default(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        html = self.html()
        self.assertEqual(self.order(), [SEEDED, other.name])
        row = text_of(row_of(html, other))
        self.assertEqual(
            row,
            "2 ↑ ↓ Banque Exemple (CSV) CSV (colonnes) date 1 · libellé 2, 3 · débits 4 · crédits 5 Virgule ( , ) "
            "aaaa-mm-jj "
            "Point (1,234.56) Modifier Supprimer",
        )
        # The first cannot go up, the last cannot go down.
        first, last = row_of(html, seeded()), row_of(html, other)
        self.assertIn("disabled", button_tag(first, "monter"))
        self.assertNotIn("disabled", button_tag(first, "descendre"))
        self.assertNotIn("disabled", button_tag(last, "monter"))
        self.assertIn("disabled", button_tag(last, "descendre"))
        self.assertEqual(text_of(html).count("par défaut"), 1)

    def test_every_action_names_its_format(self):
        html = self.html()
        for label in ("Monter", "Descendre", "Modifier", "Supprimer"):
            with self.subTest(label=label):
                self.assertIn(f'aria-label="{label} « {SEEDED} »"', html)

    def test_a_stored_format_the_check_refuses_is_said_on_its_row_and_its_page(self):
        StatementFormat.objects.filter(pk=seeded().pk).update(amount_column=None)
        sentence = "Colonne du montant : indiquez la colonne du montant, ou celles des débits et des crédits"
        row = text_of(row_of(self.html(), seeded()))
        self.assertIn(f"à corriger {sentence}", row)
        page = text_of(self.html(self.format_url(seeded())))
        self.assertIn(f"{sentence} - aucun relevé ne se lit avec ce format : corrigez-le ci-dessous.", page)

    def test_no_format_at_all(self):
        StatementFormat.objects.all().delete()
        response = self.get()
        self.assertIn(
            "Aucun format : aucun relevé ne s'importe. Ajoutez-en un ci-dessous.", text_of(response.content.decode())
        )
        self.assertEqual(response.context["rows"], [])

    def test_the_two_pages_lead_to_each_other(self):
        html = self.html()
        self.assertIn(f'<a href="{reverse("bank:recognition")}">Reconnaissance des opérations</a>', html)
        self.assertIn(f'<a href="{reverse("bank:bank_home")}">← Banque</a>', html)
        recognition_page = self.html(reverse("bank:recognition"))
        self.assertIn(f'<a href="{self.url}">Format du relevé</a>', recognition_page)

    def test_the_explainer_s_example_reads_as_it_says(self):
        example = views.FORMAT_EXAMPLE
        fmt = SimpleNamespace(
            name="Exemple",
            encoding="auto",
            amount_column=None,
            value_date_column=None,
            bank_type_column=None,
            account_pattern="",
            **example.settings,
        )
        content = "\n".join(example.rows).encode()
        read = statements.parse_statement(content, recognition.Rules(), fmt)
        self.assertEqual(
            [(line.operation_date, line.label, line.amount) for line in read.lines],
            [
                (date(2026, 7, 3), "CB EPICERIE EXEMPLE", Decimal("-12.30")),
                (date(2026, 7, 5), "VIR CLIENT EXEMPLE", Decimal("250.00")),
            ],
        )
        html = self.html()
        help_text = text_of(html[html.index('<details class="explainer pattern-help">') :])
        self.assertIn("Lire un format", help_text)
        self.assertIn("Les colonnes se comptent à partir de 1, de gauche à droite", help_text)
        self.assertIn("(?P<compte>…)", help_text)
        for row in example.rows:
            self.assertIn(row, help_text)
        self.assertIn(f"{example.said} → {example.reads}.", help_text)
        # The words say what the settings are.
        self.assertIn("débits 3, crédits 4", example.said)
        self.assertIn("-12.30 € et 250.00 €", example.reads)


class NewFormatTests(Page):
    def test_the_form_is_drawn_for_columns_and_a_file(self):
        html = self.html()
        form = self.new_form(html)
        self.assertEqual(form.attrs["enctype"], "multipart/form-data")
        self.assertEqual(
            [name for name in form.names if name not in ("csrfmiddlewaretoken", "action")],
            ["name", "file_type", *[name for name in OTHER_FORMAT if name != "name"], views.TEST_FILE],
        )
        for name in ("date_column", "amount_column", "debit_column", "credit_column", "value_date_column"):
            with self.subTest(column=name):
                control = form.control(name)
                self.assertEqual(
                    (control.attrs["type"], control.attrs["min"], control.attrs["max"]),
                    ("number", "1", str(statements.MAX_COLUMN)),
                )
        self.assertEqual(form.control("label_columns").attrs["type"], "text")
        pattern = form.control("account_pattern").attrs
        for attribute, value in (("class", "pattern-input"), ("spellcheck", "false"), ("autocomplete", "off")):
            self.assertEqual(pattern[attribute], value)
        self.assertEqual(form.control(views.TEST_FILE).attrs["type"], "file")
        # French labels, the tab offered as a separator.
        self.assertIn("Colonnes du libellé", text_of(field_of(html, "label_columns")))
        self.assertIn(("\t", False), form.control("delimiter").options)

    def test_a_new_format_is_stored_last_its_label_columns_as_the_page_prints_them(self):
        highest = StatementFormat.objects.order_by("-position").first().position
        response = self.add()
        made = StatementFormat.objects.get(name="Banque Exemple (CSV)")
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#format-{made.pk}")
        self.assertEqual(self.messages_of(response), ["Format « Banque Exemple (CSV) » ajouté."])
        self.assertEqual(
            (
                made.position,
                made.delimiter,
                made.date_format,
                made.decimal_mark,
                made.date_column,
                made.label_columns,
                made.amount_column,
                made.debit_column,
                made.credit_column,
                made.account_pattern,
            ),
            (highest + 1, ",", "yyyy-mm-dd", ".", 1, "2, 3", None, 4, 5, r"^Compte,(?P<compte>[0-9]+)"),
        )
        self.assertEqual(self.order(response), [SEEDED, made.name])
        # The default stays the first.
        self.assertEqual(reconcile.default_format().name, SEEDED)

    def test_a_tab_separated_format_keeps_its_tab(self):
        self.add(delimiter="\t", name="Banque Tabulée")
        self.assertEqual(StatementFormat.objects.get(name="Banque Tabulée").delimiter, "\t")

    def test_the_name_is_kept_as_typed_but_for_its_spaces(self):
        self.add(name="  Banque   Exemple  ")
        self.assertTrue(StatementFormat.objects.filter(name="Banque Exemple").exists())

    def assertRefused(self, response, field, sentence, errors=1):
        """Refused on the page drawn again, said once, under `field` - and
        `errors` sentences under the form's fields in all - nothing stored."""
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        html = response.content.decode()
        self.assertEqual(text_of(html).count(sentence), 1, sentence)
        form = html[html.index('id="nouveau-format"') :]
        form = form[: form.index("</form>")]
        self.assertIn(sentence, text_of(field_of(form, field)))
        self.assertIn("has-error", field_of(form, field).split(">")[0])
        self.assertEqual(form.count('class="field-error"'), errors, text_of(form))
        self.assertEqual(self.order(), [SEEDED])
        self.assertIsNone(response.context["test"])

    def test_each_refusal_is_said_on_its_own_field_in_french(self):
        cases = (
            ({"label_columns": "1"}, "label_columns", "La colonne 1 sert deux fois : pour la date et pour le libellé."),
            (
                {"label_columns": "2", "credit_column": "2"},
                "credit_column",
                "La colonne 2 sert deux fois : pour le libellé et pour les crédits.",
            ),
            (
                {"debit_column": "", "credit_column": ""},
                "amount_column",
                "Indiquez la colonne du montant, ou celles des débits et des crédits.",
            ),
            (
                {"amount_column": "6"},
                "amount_column",
                "Un montant signé OU des débits et des crédits : pas les deux (laissez l'un vide).",
            ),
            ({"label_columns": "x"}, "label_columns", "« x » n'est pas un numéro de colonne (de 1 à 50)."),
            ({"label_columns": "2, 3, 2"}, "label_columns", "La colonne 2 est indiquée deux fois."),
            ({"label_columns": ""}, "label_columns", "Indiquez au moins une colonne pour le libellé."),
            ({"date_column": ""}, "date_column", "Indiquez une colonne."),
            ({"date_column": "0"}, "date_column", "Un numéro de colonne de 1 à 50."),
            ({"debit_column": "51"}, "debit_column", "Un numéro de colonne de 1 à 50."),
            ({"value_date_column": "1,5"}, "value_date_column", "Un numéro de colonne de 1 à 50."),
            ({"name": ""}, "name", "Donnez un nom au format."),
            ({"name": "x" * 101}, "name", "100 caractères au plus (101 ici)."),
            ({"account_pattern": "Compte,("}, "account_pattern", "Motif du numéro de compte : parenthèse non fermée"),
            (
                {"account_pattern": "Compte,(?P<numero>[0-9]+)"},
                "account_pattern",
                "Motif du numéro de compte : le groupe (?P<numero>…) ne sert à rien ; seul (?P<compte>…) est lu.",
            ),
            # A NUL pasted or posted by hand, in every text field: Django's
            # own « Null characters are not allowed. » is English.
            ({"name": "Banque\x00Exemple"}, "name", NUL_SAID),
            ({"label_columns": "2\x00"}, "label_columns", NUL_SAID),
            ({"account_pattern": "^Compte,\x00"}, "account_pattern", NUL_SAID),
        )
        for values, field, sentence in cases:
            with self.subTest(field=field, values=values):
                response = self.add(**values)
                self.assertRefused(response, field, sentence)
                self.assertNotIn("Null characters", response.content.decode())

    def test_every_text_field_says_a_nul_in_french(self):
        """Every field of the form Django checks for a NUL (its CharFields)
        carries the French sentence - a text field added later included."""
        form = StatementFormatForm()
        texts = [name for name, field in form.fields.items() if isinstance(field, forms.CharField)]
        self.assertEqual(texts, ["name", "label_columns", "account_pattern"])
        for name in texts:
            with self.subTest(field=name):
                self.assertEqual(form.fields[name].error_messages["null_characters_not_allowed"], NUL_SAID)

    def test_a_choice_the_menu_does_not_offer_is_refused(self):
        form = self.new_form()
        for field, sentence in (
            ("delimiter", "Séparateur inconnu."),
            ("encoding", "Encodage inconnu."),
            ("date_format", "Format de date inconnu."),
            ("decimal_mark", "Séparateur décimal inconnu."),
        ):
            with self.subTest(field=field):
                data = as_post(form.submission(press=("action", "enregistrer"), values=OTHER_FORMAT))
                data[field] = ["~"]
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(sentence, text_of(field_of(response.content.decode(), field)))
                self.assertEqual(self.order(), [SEEDED])

    def test_a_name_another_format_has_whatever_its_case_and_accents_is_refused(self):
        self.assertRefused(
            self.add(name="bnp  paribas (CSV)"),
            "name",
            f"Le format « {SEEDED} » porte déjà ce nom : choisissez-en un autre.",
        )
        make_format("Banque Élysée", **OTHER_FIELDS)
        response = self.add(name="BANQUE ELYSEE")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Le format « Banque Élysée » porte déjà ce nom", text_of(response.content.decode()))
        self.assertEqual(StatementFormat.objects.count(), 2)

    def test_a_name_taken_meanwhile_is_said_on_the_name_never_a_500(self):
        with another_tab_takes_the_name():
            response = self.add()
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertIn("Un format porte déjà ce nom.", text_of(field_of(response.content.decode(), "name")))
        # The other tab's format alone.
        self.assertEqual(StatementFormat.objects.filter(name="Banque Exemple (CSV)").count(), 1)
        self.assertEqual(StatementFormat.objects.count(), 2)

    def test_an_unknown_action_writes_nothing(self):
        data = as_post(self.new_form().submission(press=("action", "enregistrer"), values=OTHER_FORMAT))
        for action in (["autre"], []):
            with self.subTest(action=action):
                data["action"] = action
                response = self.client.post(self.url, data, follow=True)
                self.assertEqual(self.messages_of(response), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(self.order(), [SEEDED])

    def test_a_post_without_its_token_is_refused(self):
        response = self.client.post(self.url, {"action": "enregistrer", **OTHER_FORMAT})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.order(), [SEEDED])


class TesterTests(Page):
    def test_enter_tests_and_never_saves(self):
        for url in (self.url, self.format_url(seeded())):
            with self.subTest(url=url):
                form = form_posting_to(self.html(url), url, holding=("action", "tester"))
                first = form.buttons()[0]
                self.assertEqual((first.name, first.attrs.get("value")), ("action", "tester"))

    def test_the_owner_s_file_in_numbered_columns_then_what_the_format_reads_nothing_saved(self):
        before = self.stored()
        response = self.edit(seeded(), press=("action", "tester"), file=upload(bnp_file()))
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertEqual(self.stored(), before)
        test = response.context["test"]
        self.assertEqual((test.file_name, test.width, test.count, test.known), ("releve.csv", 6, 2, 0))
        html = response.content.decode()
        text = text_of(html)
        self.assertIn("Fichier « releve.csv » : rien n'est enregistré.", text)
        # The columns are numbered as the form counts them, from 1.
        rows = table_of(html, "premières lignes du fichier")
        self.assertIn("<th>1</th>", rows)
        self.assertIn("<th>6</th>", rows)
        self.assertNotIn("<th>7</th>", rows)
        self.assertIn("1 2 3 4 5 6 Compte de ch&egrave;ques Compte de ch&amp;egrave;ques ****0042", text_of(rows))
        self.assertIn("16/07/2026 PAIEMENT CB FACTURE CARTE FACTURE CARTE DU 150726 EPICERIE EXEMPL…", text_of(rows))
        # What it reads: the account, each operation signed, with what the
        # rules read on it.
        self.assertIn("2 opérations lues, compte ****0042.", text)
        read = text_of(table_of(html, "opérations lues"))
        self.assertIn(
            "16/07/2026 PAIEMENT CB FACTURE CARTE DU 150726 EPICERIE EXEMPLE CARTE 4974XXXXXXXX1111 16/07/2026 "
            "-13.06 € Carte EPICERIE EXEMPLE",
            read,
        )
        self.assertIn("09/07/2026 PRELEVEMENT PRLV SEPA FOURNISSEUR EXEMPLE", read)
        self.assertIn("-120.35 € Prélèvement FOURNISSEUR EXEMPLE", read)
        self.assertIn('data-sort="2026-07-16"', html)
        self.assertIn('data-sort="-13.06"', html)
        self.assertNotIn("de plus", text)
        # The form keeps what was typed, the format what was stored.
        self.assertEqual(self.edit_form(seeded()).control("label_columns").value, "4")

    def test_a_file_already_imported_is_said_so(self):
        reconcile.import_statement(bnp_file())
        response = self.edit(seeded(), press=("action", "tester"), file=upload(bnp_file()))
        self.assertIn("2 opérations lues, compte ****0042 ; dont 2 déjà importées.", text_of(response.content.decode()))
        self.assertEqual(BankTransaction.objects.count(), 2)

    def test_another_bank_s_file_with_settings_not_saved(self):
        before = self.stored()
        response = self.add(press=("action", "tester"), file=upload(OTHER_BANK, "export.csv"))
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertEqual(self.stored(), before)
        test = response.context["test"]
        self.assertEqual((test.width, test.count, test.account), (5, 2, "000123456789"))
        html = response.content.decode()
        self.assertIn(
            "1 2 3 4 5 Banque Exemple - export des opérations Compte 000123456789 Date Libellé Détail",
            text_of(table_of(html, "premières lignes du fichier")),
        )
        read = text_of(table_of(html, "opérations lues"))
        self.assertIn("03/07/2026 — CB EPICERIE EXEMPLE ref 0001 — -12.30 € Autre —", read)
        # Grouped by thousands (UI conventions) - text_of collapses the no-break space.
        self.assertIn("05/07/2026 — VIR CLIENT EXEMPLE ref 0002 — 1 250.00 € Virement —", read)
        self.assertIn("2 opérations lues, compte 000123456789.", text_of(html))
        # What was typed is drawn back, to be saved once it reads right.
        form = self.new_form(html)
        self.assertEqual(
            [form.control(name).value for name in ("delimiter", "debit_column", "credit_column")], [",", "4", "5"]
        )

    def test_the_same_file_read_with_the_wrong_columns_says_why_and_still_numbers_them(self):
        response = self.edit(seeded(), press=("action", "tester"), file=upload(OTHER_BANK, "export.csv"))
        html = response.content.decode()
        self.assertIn(
            "Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV "
            f"(format « {SEEDED} »).",
            text_of(html),
        )
        # Split by « ; », each row is one column: that is what is shown.
        self.assertEqual(response.context["test"].width, 1)
        self.assertIn("premières lignes du fichier", html)
        self.assertNotIn("opérations lues", html)

    def test_a_file_in_another_encoding_is_refused_with_the_import_s_sentence(self):
        content = "03/08/2026;PAIEMENT CB;X;CAFÉ EXEMPLE;03/08/2026;-4,10\n".encode("cp1252")
        response = self.edit(seeded(), press=("action", "tester"), encoding="utf-8", file=upload(content))
        html = response.content.decode()
        self.assertIn(
            "Ce fichier n'est pas en UTF-8 : changez l'encodage du format du relevé, ou exportez-le à nouveau.",
            text_of(html),
        )
        self.assertNotIn("premières lignes du fichier", html)
        self.assertEqual(seeded().encoding, "auto")
        # Read as Windows-1252, as typed, it reads.
        response = self.edit(seeded(), press=("action", "tester"), encoding="cp1252", file=upload(content))
        self.assertIn("CAFÉ EXEMPLE", text_of(table_of(response.content.decode(), "opérations lues")))

    def test_without_a_file_the_format_is_checked_and_the_page_asks_for_one(self):
        response = self.add(press=("action", "tester"))
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertIn("Choisissez un fichier pour voir ce que le format en lit.", text_of(response.content.decode()))
        self.assertEqual(self.order(), [SEEDED])

    def test_a_refused_format_reads_nothing_and_says_its_errors(self):
        response = self.add(press=("action", "tester"), label_columns="1", file=upload(OTHER_BANK))
        self.assertIsNone(response.context["test"])
        html = response.content.decode()
        self.assertIn("La colonne 1 sert deux fois", text_of(field_of(html, "label_columns")))
        self.assertNotIn('id="essai"', html)

    def test_the_name_alone_wrong_still_reads_the_file(self):
        response = self.add(press=("action", "tester"), name=SEEDED.upper(), file=upload(OTHER_BANK))
        self.assertEqual(response.context["test"].count, 2)
        self.assertIn("porte déjà ce nom", text_of(response.content.decode()))

    def test_a_file_too_heavy_is_refused_unread(self):
        content = bnp_file()
        with mock.patch("common.UPLOAD_MAX_FILE_BYTES", 10):
            response = self.edit(seeded(), press=("action", "tester"), file=upload(content, "gros.csv"))
        text = text_of(response.content.decode())
        self.assertIn("« gros.csv » pèse", text)
        self.assertIn("au plus par fichier", text)
        self.assertNotIn("premières lignes du fichier", response.content.decode())

    def test_a_file_that_is_no_statement_file_is_refused_unread(self):
        response = self.edit(seeded(), press=("action", "tester"), file=upload(bnp_file(), "releve.pdf"))
        self.assertIn(
            "releve.pdf : seuls les relevés CSV, OFX ou CAMT.053 (XML) sont acceptés.",
            text_of(response.content.decode()),
        )
        self.assertEqual(response.context["test"].rows, [])

    def test_long_files_are_cut_and_say_how_many_more(self):
        rows = [debit_row(date(2026, 7, day), f"FOURNISSEUR {day:02d}", "1,00") for day in range(1, 21)]
        with mock.patch.object(views, "TEST_ROWS_SHOWN", 3), mock.patch.object(views, "TEST_LINES_SHOWN", 5):
            response = self.edit(seeded(), press=("action", "tester"), file=upload(statement(*rows)))
        test = response.context["test"]
        self.assertEqual((len(test.rows), test.count, len(test.lines), test.more), (3, 20, 5, 15))
        self.assertIn("… et 15 de plus.", text_of(response.content.decode()))

    def test_what_the_file_holds_is_shown_escaped(self):
        content = statement(debit_row(date(2026, 7, 9), "<b>FOURNISSEUR</b>", "1,00"))
        html = self.edit(seeded(), press=("action", "tester"), file=upload(content)).content.decode()
        self.assertNotIn("<b>FOURNISSEUR", html)
        self.assertIn("&lt;b&gt;FOURNISSEUR&lt;/b&gt;", html)

    def test_columns_past_what_a_format_may_name_are_not_numbered(self):
        content = (";".join(f"C{number}" for number in range(1, 61)) + "\n").encode()
        response = self.edit(seeded(), press=("action", "tester"), file=upload(content))
        test = response.context["test"]
        self.assertEqual((test.width, test.wider), (statements.MAX_COLUMN, True))
        html = response.content.decode()
        self.assertIn(f"<th>{statements.MAX_COLUMN}</th>", html)
        self.assertNotIn(f"<th>{statements.MAX_COLUMN + 1}</th>", html)
        self.assertIn("Les colonnes au-delà de la 50e ne sont pas affichées.", text_of(html))


class EditTests(Page):
    def test_the_format_s_page_holds_it_as_stored(self):
        form = self.edit_form(seeded())
        # « Tester sur un fichier » there too: the form posts the file.
        self.assertEqual(form.attrs["enctype"], "multipart/form-data")
        self.assertEqual(form.control(views.TEST_FILE).attrs["type"], "file")
        self.assertEqual(
            [form.control(name).value for name in ("name", *[name for name in OTHER_FORMAT if name != "name"])],
            [SEEDED, "auto", ";", "dd/mm/yyyy", ",", "1", "4", "6", "", "", "5", "2", r"\*{2,}[0-9]+"],
        )
        html = self.html(self.format_url(seeded()))
        self.assertIn(f"<h1>Format « {SEEDED} »</h1>", html.replace("&#x27;", "'"))
        self.assertIn(f'<a href="{self.url}">← Format du relevé</a>', html)

    def test_an_edit_is_saved(self):
        fmt = seeded()
        response = self.edit(fmt, label_columns="3 4", name="BNP Paribas")
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#format-{fmt.pk}")
        self.assertEqual(self.messages_of(response), ["Format « BNP Paribas » enregistré."])
        fmt.refresh_from_db()
        self.assertEqual((fmt.name, fmt.label_columns, fmt.position), ("BNP Paribas", "3, 4", 1))

    def test_a_format_keeps_its_own_name_in_another_case(self):
        self.edit(seeded(), name=SEEDED.upper())
        self.assertTrue(StatementFormat.objects.filter(name=SEEDED.upper()).exists())

    def test_a_refused_edit_writes_nothing_and_the_page_keeps_the_stored_name(self):
        make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        before = self.stored()
        response = self.edit(seeded(), name="banque exemple (csv)", amount_column="")
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertEqual(self.stored(), before)
        html = response.content.decode()
        self.assertIn(f"<h1>Format « {SEEDED} »</h1>", html)
        self.assertEqual(text_of(html).count("Le format « Banque Exemple (CSV) » porte déjà ce nom"), 1)
        self.assertEqual(text_of(html).count("Indiquez la colonne du montant"), 1)

    def test_a_name_taken_meanwhile_is_said_on_the_name_never_a_500(self):
        with another_tab_takes_the_name():
            response = self.edit(seeded(), name="Banque Exemple (CSV)")
        self.assertEqual((response.status_code, response.redirect_chain), (200, []))
        self.assertIn("Un format porte déjà ce nom.", text_of(response.content.decode()))
        self.assertEqual(seeded().name, SEEDED)

    def test_tester_on_the_page_reads_with_the_settings_as_typed(self):
        response = self.edit(seeded(), press=("action", "tester"), file=upload(OTHER_BANK), **OTHER_FORMAT)
        self.assertEqual(response.context["test"].count, 2)
        self.assertEqual(seeded().delimiter, ";")


class MoveTests(Page):
    def setUp(self):
        super().setUp()
        self.other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)

    def test_up_and_down_swap_and_the_first_is_the_import_s(self):
        response = self.press(self.other, "monter")
        self.assertEqual(
            self.messages_of(response),
            ["Format « Banque Exemple (CSV) » monté : c'est maintenant celui de l'import."],
        )
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#format-{self.other.pk}")
        self.assertEqual(self.order(response), [self.other.name, SEEDED])
        self.assertEqual(reconcile.default_format().pk, self.other.pk)
        response = self.press(self.other, "descendre")
        self.assertEqual(self.messages_of(response), ["Format « Banque Exemple (CSV) » descendu."])
        self.assertEqual(self.order(response), [SEEDED, self.other.name])

    def test_the_ends_move_nothing(self):
        token = self.token()
        positions = list(StatementFormat.objects.values_list("name", "position"))
        for fmt, action, said in ((seeded(), "monter", "le premier"), (self.other, "descendre", "le dernier")):
            with self.subTest(action=action):
                with self.assertRaises(AssertionError):
                    self.button_form(fmt, action).submission(press=("action", action))
                response = self.client.post(
                    self.format_url(fmt), {"action": action, "csrfmiddlewaretoken": token}, follow=True
                )
                self.assertEqual(self.messages_of(response), [f"Format « {fmt.name} » déjà {said}."])
        self.assertEqual(list(StatementFormat.objects.values_list("name", "position")), positions)

    def test_formats_sharing_a_position_are_ordered_by_name_and_moved_one_at_a_time(self):
        third = make_format("Autre Banque", position=1, **OTHER_FIELDS)
        StatementFormat.objects.update(position=1)
        # By name, as the database compares it: never by id.
        self.assertEqual(self.order(), [third.name, SEEDED, self.other.name])
        response = self.press(seeded(), "monter")
        self.assertEqual(
            self.messages_of(response), [f"Format « {SEEDED} » monté : c'est maintenant celui de l'import."]
        )
        self.assertEqual(self.order(), [SEEDED, third.name, self.other.name])
        self.assertEqual(sorted(StatementFormat.objects.values_list("position", flat=True)), [1, 2, 3])
        self.assertEqual(reconcile.default_format().name, SEEDED)


class DeleteTests(Page):
    def test_delete_asks_first_then_deletes(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        form = self.button_form(other, "supprimer")
        self.assertEqual(form.attrs["data-confirm"], "Supprimer le format « Banque Exemple (CSV) » ?")
        response = self.send(form, press=("action", "supprimer"))
        self.assertEqual(self.messages_of(response), ["Format « Banque Exemple (CSV) » supprimé."])
        self.assertEqual(response.redirect_chain[-1][0], self.url)
        self.assertEqual(self.order(response), [SEEDED])

    def test_the_default_deleted_the_next_one_is_the_import_s(self):
        other = make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        self.press(seeded(), "supprimer")
        self.assertEqual(reconcile.default_format().pk, other.pk)
        self.assertIn("par défaut", text_of(row_of(self.html(), other)))

    def test_the_last_format_is_never_deleted(self):
        before = self.stored()
        response = self.press(seeded(), "supprimer")
        self.assertEqual(self.messages_of(response), ["Gardez au moins un format : modifiez-le plutôt."])
        self.assertEqual(response.redirect_chain[-1][0], f"{self.url}#format-{seeded().pk}")
        self.assertEqual(self.stored(), before)


class StateTests(Page):
    def test_a_get_writes_nothing(self):
        make_format("Banque Exemple (CSV)", **OTHER_FIELDS)
        before = self.stored()
        for action in ("supprimer", "monter", "descendre", "enregistrer", "tester"):
            with self.subTest(action=action):
                response = self.client.get(self.format_url(seeded()), {"action": action, "name": "X"})
                self.assertEqual(response.status_code, 200)
                response = self.client.get(self.url, {"action": action, **OTHER_FORMAT, "name": "Y"})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored(), before)

    def test_an_unknown_action_or_none_writes_nothing(self):
        before = self.stored()
        token = self.token()
        for action in ("autre", None):
            with self.subTest(action=action):
                data = {"csrfmiddlewaretoken": token, **({"action": action} if action else {})}
                response = self.client.post(self.format_url(seeded()), data, follow=True)
                self.assertEqual(self.messages_of(response), ["Action inconnue : rien n'a été modifié."])
        self.assertEqual(self.stored(), before)

    def test_a_format_that_is_not_there_is_a_404(self):
        url = reverse("bank:statement_format", args=[999999])
        self.get(url, status=404)
        response = self.client.post(url, {"action": "supprimer", "csrfmiddlewaretoken": self.token()})
        self.assertEqual(response.status_code, 404)

    def test_a_post_without_its_token_is_refused(self):
        response = self.client.post(self.format_url(seeded()), {"action": "supprimer"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(StatementFormat.objects.filter(name=SEEDED).exists())


#: An OFX format as stored: no column, no account pattern, the CSV-only
#: choices at the model's defaults (forms.CANONICAL).
OFX_FIELDS = {"file_type": "ofx", "date_column": None, "label_columns": "", "account_pattern": ""}


def as_the_script_leaves_it(html: str) -> str:
    """The page once static/js/statement_format.js has followed the menu to
    a file that says where each datum is: the CSV's fieldset hidden and
    disabled (the test client runs no script)."""
    marker = 'data-file-types="csv"'
    assert html.count(marker) == 1
    return html.replace(marker, f"{marker} hidden disabled")


class FileTypeTests(Page):
    """A format of another kind than CSV saved FROM THE PAGE, as a browser
    sends it: the CSV's fields are in a fieldset the script disables (and
    the server, for the kind it draws), so none of them is posted - and the
    form must save all the same, storing nothing in them."""

    CANONICAL_OFX = {
        "file_type": "ofx",
        "delimiter": ";",
        "date_format": "dd/mm/yyyy",
        "decimal_mark": ",",
        "date_column": None,
        "label_columns": "",
        "amount_column": None,
        "debit_column": None,
        "credit_column": None,
        "value_date_column": None,
        "bank_type_column": None,
        "account_pattern": "",
    }

    def stored_fields(self, name) -> dict:
        made = StatementFormat.objects.get(name=name)
        return {field: getattr(made, field) for field in self.CANONICAL_OFX}

    def test_the_kind_of_file_comes_first_and_a_csv_s_fields_are_apart(self):
        html = self.html()
        form = self.new_form(html)
        self.assertEqual(form.control("file_type").options, [("csv", True), ("ofx", False), ("camt053", False)])
        self.assertIn("Type de fichier", text_of(field_of(html, "file_type")))
        fieldset = html[html.index('<fieldset class="format-columns" data-file-types="csv">') :]
        fieldset = fieldset[: fieldset.index("</fieldset>")]
        for name in ("delimiter", "date_format", "decimal_mark", "date_column", "label_columns", "account_pattern"):
            with self.subTest(field=name):
                self.assertIn(f'name="{name}"', fieldset)
        for name in ("name", "file_type", "encoding", views.TEST_FILE):
            with self.subTest(field=name):
                self.assertNotIn(f'name="{name}"', fieldset)
        # Its own script, never ui.js's.
        self.assertIn("js/statement_format.js", html)

    def test_a_new_ofx_format_is_saved_from_the_page_its_csv_fields_never_sent(self):
        form = self.new_form(as_the_script_leaves_it(self.html()))
        values = {"name": "Relevé OFX", "file_type": "ofx"}
        sent = form.submission(press=("action", "enregistrer"), values=values)
        self.assertEqual(
            sorted({name for name, _value in sent}), ["action", "csrfmiddlewaretoken", "encoding", "file_type", "name"]
        )
        response = self.send(form, press=("action", "enregistrer"), values=values)
        self.assertEqual(self.messages_of(response), ["Format « Relevé OFX » ajouté."])
        self.assertEqual(self.stored_fields("Relevé OFX"), self.CANONICAL_OFX)
        row = text_of(row_of(response.content.decode(), StatementFormat.objects.get(name="Relevé OFX")))
        self.assertIn("Relevé OFX OFX / QFX (Money) lues dans le fichier — — — Modifier", row)

    def test_without_the_script_what_a_csv_s_fields_hold_is_not_read_nor_stored(self):
        response = self.add(
            name="Relevé CAMT",
            file_type="camt053",
            date_column="x",
            label_columns="y",
            debit_column="51",
            account_pattern="(",
        )
        self.assertEqual(self.messages_of(response), ["Format « Relevé CAMT » ajouté."])
        self.assertEqual(self.stored_fields("Relevé CAMT"), {**self.CANONICAL_OFX, "file_type": "camt053"})

    def test_a_stored_ofx_format_s_page_draws_its_csv_fields_disabled_and_saves(self):
        fmt = make_format("Relevé OFX", **OFX_FIELDS)
        html = self.html(self.format_url(fmt))
        self.assertIn('<fieldset class="format-columns" data-file-types="csv" hidden disabled>', html)
        form = self.edit_form(fmt)
        self.assertTrue(form.control("date_column").disabled)
        self.assertFalse(form.control("encoding").disabled)
        response = self.edit(fmt, name="Relevé OFX de la banque", encoding="cp1252")
        self.assertEqual(self.messages_of(response), ["Format « Relevé OFX de la banque » enregistré."])
        fmt.refresh_from_db()
        self.assertEqual((fmt.encoding, fmt.file_type, fmt.date_column), ("cp1252", "ofx", None))
        # « Tester » reads with it too: none of a CSV's fields is asked for.
        response = self.edit(fmt, press=("action", "tester"))
        self.assertEqual(response.context["test"].problem, views.NO_TEST_FILE)

    def test_turned_into_a_csv_it_asks_for_its_columns(self):
        fmt = make_format("Relevé OFX", **OFX_FIELDS)
        # Without the script: the fieldset stays as the server drew it, and
        # nothing of it is sent.
        response = self.edit(fmt, file_type="csv")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Indiquez une colonne.", text_of(field_of(html, "date_column")))
        fmt.refresh_from_db()
        self.assertEqual(fmt.file_type, "ofx")
        # Drawn again as the kind chosen: its columns shown, to be typed.
        self.assertIn('<fieldset class="format-columns" data-file-types="csv">', html)

    def test_an_unknown_kind_of_file_is_refused_on_its_field(self):
        form = self.new_form()
        data = as_post(form.submission(press=("action", "enregistrer"), values=OTHER_FORMAT))
        data["file_type"] = ["pdf"]
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Type de fichier inconnu.", text_of(field_of(response.content.decode(), "file_type")))
        self.assertEqual(self.order(), [SEEDED])

    def test_tester_reads_an_ofx_file_with_no_column_to_number(self):
        fmt = make_format("Relevé OFX", **OFX_FIELDS)
        response = self.edit(fmt, press=("action", "tester"), file=upload(ofx_files.sgml(), "releve.ofx"))
        test = response.context["test"]
        self.assertEqual((test.rows, test.refusal, test.count), ([], "", len(ofx_files.OPERATIONS)))
        self.assertEqual(test.account, ofx_files.ACCOUNT)
        html = response.content.decode()
        self.assertNotIn("Premières lignes, colonnes numérotées", html)
        self.assertIn("Ce que le format lit", html)

    def test_tester_refuses_a_file_of_another_kind_in_the_import_s_words(self):
        response = self.edit(seeded(), press=("action", "tester"), file=upload(ofx_files.xml(), "releve.csv"))
        self.assertTrue(response.context["test"].refusal.startswith("Ce fichier est un relevé OFX / QFX (Money)"))

    def test_both_file_inputs_offer_every_kind_of_statement_file(self):
        for url in (self.url, self.format_url(seeded())):
            with self.subTest(url=url):
                self.assertIn(
                    f'name="fichier_essai" id="id_fichier_essai" accept="{statements.ACCEPT_ATTRIBUTE}"', self.html(url)
                )

    def test_the_form_s_own_errors_are_said_once(self):
        refusal = statements.FormatError("ailleurs", "Phrase d'essai.")
        with mock.patch.object(statements, "check_format", side_effect=refusal):
            response = self.add()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(text_of(response.content.decode()).count("Phrase d'essai."), 1)
