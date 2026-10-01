from django import forms
from django.forms.models import construct_instance

from returnables.forms import PATTERN_ATTRS
from returnables.patterns import PatternError

from . import recognition
from .models import IgnoreRule, OperationRule

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
                "max_length": "%(limit_value)d caractères au plus (%(show_value)d ici).",
                "unique": NAME_TAKEN,
            },
            "meaning": {"required": MEANING_REQUIRED, "invalid_choice": MEANING_REQUIRED},
            "searched": {"required": SEARCHED_REQUIRED, "invalid_choice": SEARCHED_REQUIRED},
            "pattern": {
                "required": PATTERN_REQUIRED,
                "max_length": "%(limit_value)d caractères au plus (%(show_value)d ici).",
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
