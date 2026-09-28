from django import forms

from .models import IgnoreRule


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
