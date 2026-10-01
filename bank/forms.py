from types import SimpleNamespace

from django import forms
from django.forms.models import construct_instance

from returnables.forms import PATTERN_ATTRS
from returnables.patterns import PatternError

from . import recognition, statements
from .models import IgnoreRule, OperationRule, StatementFormat

Meaning = OperationRule.Meaning

#: The meanings in the menu, by the question they answer: what the operation
#: is (read at import, stored) and what a credit is in the till (read when
#: « Entrées d'argent » is drawn).
MEANING_GROUPS = (
    (
        "Nature de l'opération (lue à l'import)",
        [(meaning.value, meaning.label) for meaning in Meaning if meaning.value in recognition.KIND_MEANINGS],
    ),
    (
        "En caisse (entrées d'argent)",
        [(meaning.value, meaning.label) for meaning in Meaning if meaning.value in recognition.TILL_MEANINGS],
    ),
)
NAME_REQUIRED = "Donnez un nom à la règle."
PATTERN_REQUIRED = "Écrivez le motif cherché."
MEANING_REQUIRED = "Choisissez ce que signifie l'opération trouvée."
SEARCHED_REQUIRED = "Choisissez le libellé ou le type d'opération."
#: A name another rule took between the check and the save (two tabs, a
#: double click): said on the field rather than a 500.
NAME_TAKEN = "Une règle porte déjà ce nom."
#: A NUL in a text field (pasted, or posted by hand), refused by the
#: validator Django puts on every CharField - whose own sentence is English
#: (the site's language setting is). Every text field of these forms says it.
NUL_REFUSED = "Caractère interdit (NUL) : retapez ce champ."
TOO_LONG = "%(limit_value)d caractères au plus (%(show_value)d ici)."


class IgnoreRuleForm(forms.ModelForm):
    class Meta:
        model = IgnoreRule
        fields = ["pattern", "description", "category"]
        labels = {"pattern": "Motif", "description": "Nom", "category": "Catégorie"}
        help_texts = {
            "pattern": (
                "Expression régulière cherchée dans le libellé complet de l'opération, sans tenir compte des "
                "majuscules. Ex : ECHEANCE PRET, URSSAF, DGFIP|MALAKOFF."
            ),
            "description": "Pour s'y retrouver : « Prêt », « Cotisations sociales »…",
            "category": (
                "Facultatif : ce que ces dépenses comptent comme sur « Dépenses par catégorie ». Une dépense "
                "classée à la main garde sa catégorie, la règle ne l'écrase pas."
            ),
        }
        widgets = {
            # The same free-typed-against-a-datalist field as elsewhere (see
            # StockType.category): the words a person already uses are the
            # ones they will look for, and a list of categories to keep up
            # to date is one more thing to keep up to date.
            "category": forms.TextInput(attrs={"list": "spending-category-datalist", "autocomplete": "off"}),
        }

    def clean_category(self):
        """Stored the way every other place stores a category
        (`spending.clean_category`: `rule_action`, `set_category`). The
        field trimmed the ends and nothing else, so « Frais  bancaires »
        with two spaces reached the pie as a name the address could never
        ask for again."""
        from .spending import clean_category

        return clean_category(self.cleaned_data.get("category", ""))


class OperationRuleForm(forms.ModelForm):
    """One rule of « Reconnaissance des opérations »: its name, what it means,
    where its pattern is searched, and the pattern - checked here by
    `recognition.check`, with the meaning it goes with. Its place in the
    order is the page's business (a new rule comes last of its kind;
    « monter » / « descendre » move it), never typed."""

    class Meta:
        model = OperationRule
        fields = ["name", "meaning", "searched", "pattern"]
        labels = {"name": "Nom", "meaning": "Signifie", "searched": "Cherché dans", "pattern": "Motif"}
        help_texts = {
            "name": "Ex. « Versement TPE (REMISE CB) ».",
            "pattern": "Expression régulière ; majuscules et minuscules confondues.",
        }
        error_messages = {
            "name": {
                "required": NAME_REQUIRED,
                "max_length": TOO_LONG,
                "unique": NAME_TAKEN,
                "null_characters_not_allowed": NUL_REFUSED,
            },
            "meaning": {"required": MEANING_REQUIRED, "invalid_choice": MEANING_REQUIRED},
            "searched": {"required": SEARCHED_REQUIRED, "invalid_choice": SEARCHED_REQUIRED},
            "pattern": {
                "required": PATTERN_REQUIRED,
                "max_length": TOO_LONG,
                "null_characters_not_allowed": NUL_REFUSED,
            },
        }
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
            "pattern": forms.TextInput(attrs=dict(PATTERN_ATTRS)),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Grouped by the question each meaning answers - with nothing chosen
        # first: a meaning picked for the reader is a rule that says
        # something nobody said.
        self.fields["meaning"].choices = [("", "— choisissez —"), *MEANING_GROUPS]
        #: The pattern as `recognition.check` compiled it, once the form is
        #: valid - what « Tester » runs.
        self.compiled = None

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError(NAME_REQUIRED)
        key = recognition.name_key(name)
        for other in OperationRule.objects.exclude(pk=self.instance.pk).only("pk", "name"):
            if recognition.name_key(other.name) == key:
                raise forms.ValidationError(f"La règle « {other.name} » porte déjà ce nom : choisissez-en un autre.")
        return name

    def clean(self):
        cleaned = super().clean()
        meaning, searched, pattern = (cleaned.get(name) for name in ("meaning", "searched", "pattern"))
        if meaning and searched and pattern:
            try:
                self.compiled = recognition.check(meaning, searched, pattern)
            except PatternError as error:
                self.add_error("pattern", error.message)
        return cleaned

    def _post_clean(self):
        # Every field is checked above as the model would (lengths, choices,
        # the name, the pattern by `recognition.check`). The model's own
        # clean() checks the pattern again - on what the instance held before
        # where a field was refused here (« le motif est vide » beside « Écrivez
        # le motif »): two sentences, one about a value nobody typed.
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)


FORMAT_NAME_REQUIRED = "Donnez un nom au format."
#: A name another format took between the check and the save.
FORMAT_NAME_TAKEN = "Un format porte déjà ce nom."
COLUMN_NUMBER = f"Un numéro de colonne de 1 à {statements.MAX_COLUMN}."
#: What `statements.check_format` reads, the name aside: a refusal of any of
#: them is said on its field, and the check is not run past one already said.
FORMAT_FIELDS = (
    "encoding",
    "delimiter",
    "date_format",
    "decimal_mark",
    "date_column",
    "label_columns",
    "amount_column",
    "debit_column",
    "credit_column",
    "value_date_column",
    "bank_type_column",
    "account_pattern",
)


def _column(label, *, required=False, help_text=""):
    """A column number: a number field from 1 to `statements.MAX_COLUMN`,
    every refusal in the sentence `check_format` says - the site speaks
    English to Django, whose own would reach the page."""
    return forms.IntegerField(
        label=label,
        required=required,
        min_value=1,
        max_value=statements.MAX_COLUMN,
        help_text=help_text,
        error_messages={
            "required": "Indiquez une colonne.",
            "invalid": COLUMN_NUMBER,
            "min_value": COLUMN_NUMBER,
            "max_value": COLUMN_NUMBER,
        },
    )


class StatementFormatForm(forms.ModelForm):
    """One format of « Format du relevé »: how a bank's CSV export is laid
    out - checked here by `statements.check_format`, each refusal on its
    field. Its place in the order is the page's business (a new format comes
    last; « monter » / « descendre » move it), never typed."""

    date_column = _column("Colonne de la date", required=True, help_text="Les colonnes se comptent à partir de 1.")
    amount_column = _column(
        "Colonne du montant", help_text="Signé : négatif quand l'argent sort. Sinon, débits et crédits."
    )
    debit_column = _column("Colonne des débits", help_text="À la place d'un montant signé.")
    credit_column = _column("Colonne des crédits", help_text="À la place d'un montant signé.")
    value_date_column = _column("Colonne de la date de valeur", help_text="Facultatif.")
    bank_type_column = _column("Colonne du type d'opération", help_text="Facultatif.")

    class Meta:
        model = StatementFormat
        fields = ["name", *FORMAT_FIELDS]
        labels = {
            "name": "Nom",
            "encoding": "Encodage",
            "delimiter": "Séparateur",
            "date_format": "Format des dates",
            "decimal_mark": "Séparateur décimal",
            "label_columns": "Colonnes du libellé",
            "account_pattern": "Motif du numéro de compte",
        }
        help_texts = {
            "name": "Ex. « Banque Exemple (CSV) ».",
            "label_columns": "Une colonne, ou plusieurs : « 4 » ou « 3, 4 ».",
            "account_pattern": (
                "Facultatif. Cherché au-dessus des opérations ; (?P<compte>…) n'en garde qu'une partie."
            ),
        }
        error_messages = {
            "name": {
                "required": FORMAT_NAME_REQUIRED,
                "max_length": TOO_LONG,
                "unique": FORMAT_NAME_TAKEN,
                "null_characters_not_allowed": NUL_REFUSED,
            },
            "encoding": {"required": "Encodage inconnu.", "invalid_choice": "Encodage inconnu."},
            "delimiter": {"required": "Séparateur inconnu.", "invalid_choice": "Séparateur inconnu."},
            "date_format": {"required": "Format de date inconnu.", "invalid_choice": "Format de date inconnu."},
            "decimal_mark": {
                "required": "Séparateur décimal inconnu.",
                "invalid_choice": "Séparateur décimal inconnu.",
            },
            "label_columns": {
                "required": "Indiquez au moins une colonne pour le libellé.",
                "max_length": TOO_LONG,
                "null_characters_not_allowed": NUL_REFUSED,
            },
            "account_pattern": {"max_length": TOO_LONG, "null_characters_not_allowed": NUL_REFUSED},
        }
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
            "label_columns": forms.TextInput(attrs={"autocomplete": "off"}),
            "account_pattern": forms.TextInput(attrs=dict(PATTERN_ATTRS)),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: The format as `check_format` compiled it, once every field but
        #: the name passed - what « Tester » reads the file with.
        self.layout = None

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError(FORMAT_NAME_REQUIRED)
        key = recognition.name_key(name)
        for other in StatementFormat.objects.exclude(pk=self.instance.pk).only("pk", "name"):
            if recognition.name_key(other.name) == key:
                raise forms.ValidationError(f"Le format « {other.name} » porte déjà ce nom : choisissez-en un autre.")
        return name

    def clean(self):
        cleaned = super().clean()
        # A field refused already is said once, on itself: the check would
        # read its absence as a second refusal of a value nobody typed.
        if any(name in self.errors for name in FORMAT_FIELDS):
            return cleaned
        values = SimpleNamespace(name=cleaned.get("name") or "", **{name: cleaned.get(name) for name in FORMAT_FIELDS})
        try:
            self.layout = statements.check_format(values)
        except statements.FormatError as error:
            self.add_error(error.field if error.field in self.fields else None, error.message)
            return cleaned
        # Stored as the page prints it: « 3,4 » and « 3 4 » are « 3, 4 ».
        cleaned["label_columns"] = ", ".join(str(number + 1) for number in self.layout.labels)
        return cleaned

    def _post_clean(self):
        # Every field is checked above as the model would (lengths, choices,
        # the name, the columns and the pattern by `check_format`); the
        # model's own clean() would check the format again - on what the
        # instance held before where a field was refused here.
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)
