from types import SimpleNamespace

from django import forms
from django.forms.models import construct_instance

from common import format_money
from returnables.forms import PATTERN_ATTRS
from returnables.patterns import PatternError

from . import recognition, rules, statements, treasury
from .models import IgnoreRule, OperationRule, StatementFormat, TreasuryAdjustment

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
        error_messages = {
            "pattern": {
                "required": PATTERN_REQUIRED,
                "max_length": TOO_LONG,
                "null_characters_not_allowed": NUL_REFUSED,
            },
        }
        widgets = {
            # The same free-typed-against-a-datalist field as elsewhere (see
            # StockType.category): the words a person already uses are the
            # ones they will look for, and a list of categories to keep up
            # to date is one more thing to keep up to date.
            "category": forms.TextInput(attrs={"list": "spending-category-datalist", "autocomplete": "off"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: The pattern as `rules.searcher` compiled it, once the form is
        #: valid - what « Tester » and « Ajouter la règle » search with.
        self.searcher = None

    def clean_pattern(self):
        """Through the guard of `returnables.patterns` (`rules.check`), as
        the model's own `clean` checks it - the refusal in French on this
        field, and nothing compiled that could freeze the machine."""
        pattern = self.cleaned_data["pattern"]
        try:
            self.searcher = rules.searcher(pattern)
        except PatternError as error:
            raise forms.ValidationError(error.message) from None
        return pattern

    def _post_clean(self):
        # The pattern is checked above as the model would check it; its own
        # clean() checks it again - on the value the instance held before
        # where the field was refused here: two sentences, one about a value
        # nobody typed (as OperationRuleForm).
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)

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
    "file_type",
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


#: Drawn first, whatever the kind of file.
HEAD_FIELDS = ("name", "file_type", "encoding")
#: A CSV's alone (`FileType.CSV`): drawn in a fieldset the page hides and
#: disables for another kind of file (static/js/statement_format.js, and the
#: server for the kind it draws) - a disabled field is never sent.
CSV_FIELDS = tuple(name for name in FORMAT_FIELDS if name not in HEAD_FIELDS)
#: What a CSV cannot do without, required of it alone - asked by the form as
#: before (the browser's own `required` while the fieldset is shown).
CSV_REQUIRED = ("delimiter", "date_format", "decimal_mark", "date_column", "label_columns")
#: What a format of another kind stores in the fields it does not read: the
#: model's defaults, and nothing in its columns or its account pattern - so a
#: stored row says nothing it does not mean.
CANONICAL = {
    "delimiter": StatementFormat.Delimiter.SEMICOLON.value,
    "date_format": StatementFormat.DateFormat.DAY_MONTH_YEAR.value,
    "decimal_mark": StatementFormat.DecimalMark.COMMA.value,
    "date_column": None,
    "label_columns": "",
    "amount_column": None,
    "debit_column": None,
    "credit_column": None,
    "value_date_column": None,
    "bank_type_column": None,
    "account_pattern": "",
}


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
    """One format of « Format du relevé »: the kind of file a bank exports
    and, for a CSV, how it is laid out - checked here by
    `statements.check_format`, each refusal on its field. Its place in the
    order is the page's business (a new format comes last; « monter » /
    « descendre » move it), never typed.

    A format of another kind than CSV names no column: the page sends none
    of a CSV's fields for it (their fieldset is disabled), so none is
    required of it, a refusal of one is not said, and `clean` stores
    `CANONICAL` in every one - whatever a page without its script sent."""

    head_fields = HEAD_FIELDS

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
            "file_type": "Type de fichier",
            "encoding": "Encodage",
            "delimiter": "Séparateur",
            "date_format": "Format des dates",
            "decimal_mark": "Séparateur décimal",
            "label_columns": "Colonnes du libellé",
            "account_pattern": "Motif du numéro de compte",
        }
        help_texts = {
            "name": "Ex. « Banque Exemple (CSV) ».",
            "file_type": "CSV : vous indiquez les colonnes. OFX et CAMT.053 : le fichier dit où est chaque donnée.",
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
            "file_type": {"required": "Type de fichier inconnu.", "invalid_choice": "Type de fichier inconnu."},
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
        #: The kind of file the page draws: the one posted when it is one
        #: the model offers, else the stored one - a CSV's fields are drawn
        #: hidden and disabled for another (statement_format.js follows the
        #: menu from there).
        self.shown_file_type = self.instance.file_type or StatementFormat.FileType.CSV
        posted = self.data.get(self.add_prefix("file_type")) if self.is_bound else None
        if posted in StatementFormat.FileType.values:
            self.shown_file_type = posted
        for name in CSV_REQUIRED:
            self.fields[name].required = self.shown_file_type == StatementFormat.FileType.CSV
        # Not sent (a page drawn before the kind of file existed, a request
        # written by hand): the format's own kind, never a guess.
        self.fields["file_type"].required = False

    @property
    def reads_columns(self) -> bool:
        """Whether the page draws a CSV's fields enabled."""
        return self.shown_file_type == StatementFormat.FileType.CSV

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
        if not cleaned.get("file_type") and "file_type" not in self.errors:
            cleaned["file_type"] = self.instance.file_type or StatementFormat.FileType.CSV
        file_type = cleaned.get("file_type")
        if file_type and file_type != StatementFormat.FileType.CSV:
            # None of a CSV's fields is read: a page without its script sent
            # them all the same, and what it sent there says nothing.
            for name in CSV_FIELDS:
                self._errors.pop(name, None)
            cleaned.update(CANONICAL)
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
        if self.layout.file_type == StatementFormat.FileType.CSV:
            cleaned["label_columns"] = ", ".join(str(number + 1) for number in self.layout.labels)
        return cleaned

    def _post_clean(self):
        # Every field is checked above as the model would (lengths, choices,
        # the name, the columns and the pattern by `check_format`); the
        # model's own clean() would check the format again - on what the
        # instance held before where a field was refused here.
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)


#: The widest balance read before `treasury.read_balance` says « illisible »:
#: « -9 999 999 999,99 » and its spaces fit, and no typed figure is read whole
#: past it.
BALANCE_MAX_LENGTH = 40
#: An adjustment's reason, as wide as its column.
REASON_MAX_LENGTH = TreasuryAdjustment._meta.get_field("reason").max_length  # ty: ignore[unresolved-attribute] - a CharField


class TreasuryPointForm(forms.Form):
    """« Saisir un solde » on « Trésorerie »: a day and the balance the bank
    showed for it - its field names are the page's HTTP interface (`date`,
    `solde`).

    A plain Form, never a ModelForm: the model's unique date would be refused
    by `validate_unique` before the page could offer « Remplacer » (a date
    that already has a balance is never replaced without that button - the
    view decides). The date is ISO only, between 01/01/2000 and `today`
    (`treasury.check_point_date`: en-us would read « 02/10/2026 » as 10
    February, and a future point would move the headline into the future);
    the balance is signed - an overdraft is negative - and read by
    `treasury.read_balance` (« 12.500 » asked again rather than read as
    12,50 €). Every refusal in French, on its field; a NUL is
    `NUL_REFUSED`, as on the other bank forms."""

    date = forms.CharField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"required": treasury.DATE_UNREADABLE, "null_characters_not_allowed": NUL_REFUSED},
    )
    solde = forms.CharField(
        label="Solde (€)",
        max_length=BALANCE_MAX_LENGTH,
        # No inputmode="decimal" (treasury.html says why): an overdraft is
        # typed « -250 ».
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "1 234,56"}),
        error_messages={
            "required": treasury.BALANCE_UNREADABLE,
            "max_length": treasury.BALANCE_UNREADABLE,
            "null_characters_not_allowed": NUL_REFUSED,
        },
    )

    def __init__(self, *args, today, **kwargs):
        super().__init__(*args, **kwargs)
        #: The caller's `timezone.localdate()`: the latest day a point may
        #: have, and the date input's `max`.
        self.today = today
        self.fields["date"].widget.attrs["max"] = today.isoformat()
        #: The day typed when it already has a balance - a refusal on the
        #: date (`refuse_taken`) - so the page offers « Remplacer ».
        self.taken_day = None
        #: Whether the page offers « Remplacer » beside « Enregistrer »: the
        #: view says, once it knows the date has a balance.
        self.replacing = False

    def clean_date(self):
        day, refusal = treasury.check_point_date(self.cleaned_data["date"], today=self.today)
        if refusal:
            raise forms.ValidationError(refusal)
        return day

    def clean_solde(self):
        balance, refusal = treasury.read_balance(self.cleaned_data["solde"])
        if refusal:
            raise forms.ValidationError(refusal)
        return balance

    def refuse_taken(self, day, balance) -> None:
        """« Le 01/09/2026 a déjà un solde : 1 000.00 €. », on the date: what
        « Enregistrer » says on a date that has another balance - or that
        another tab gave one between the check and the write. `balance` is
        None when that point is gone again."""
        self.taken_day = day
        said = f"Le {day:%d/%m/%Y} a déjà un solde"
        self.add_error("date", f"{said} : {format_money(balance)} €." if balance is not None else f"{said}.")


class TreasuryAdjustmentForm(forms.Form):
    """The reason an adjustment of « Écarts à résoudre » may carry - optional,
    as wide as its column. The rest of that POST (the two points, the gap
    shown) is read by hand and trusted for nothing (`views.
    treasury_adjustment_add`). Never « motif »: a regular expression on the
    bank's pages."""

    raison = forms.CharField(
        label="Raison",
        required=False,
        max_length=REASON_MAX_LENGTH,
        error_messages={"max_length": TOO_LONG, "null_characters_not_allowed": NUL_REFUSED},
    )
